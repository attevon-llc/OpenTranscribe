"""Profile-assignment workflow shared by speaker update, verify and merge."""

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.user import User
from app.schemas.media import SpeakerUpdate
from app.services.opensearch_service import update_speaker_display_name
from app.services.permission_service import org_scope_pred
from app.services.platform_bypass import PlatformBypass
from app.services.privacy.redact_view import PrivacyView
from app.services.speaker_cleanup import reconcile_speaker_impact
from app.utils.speaker_identity_detail import ORIGIN_PROFILE_CREATED
from app.utils.speaker_identity_source import record_human_identity

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

# --- Helper functions for update_speaker ---


def _handle_profile_embedding_updates(
    db: Session,
    speaker_id: int,
    old_profile_id: int | None,
    new_profile_id: int | None,
    display_name_changed: bool,
) -> list[dict[str, Any]]:
    """Rebuild the profiles (and clusters) a speaker's profile change touched.

    A speaker moving from one profile to another, or out of one, leaves the old
    profile's embedding averaging its voice; it used to be rebuilt only when the speaker
    had been auto-labeled, so a manual move left the voice in the old profile (issue
    #1214). Both sides are now rebuilt by ``speaker_cleanup``, as are the speaker's
    clusters, whose centroids weigh profile-linked members double. A rename within the
    same profile still refreshes that profile (its document carries the name).

    Returns:
        The cleanup's residuals; empty when every aggregate was rebuilt or erased.
    """
    from app.services.speaker_cleanup import SpeakerImpact
    from app.services.speaker_cleanup import profile_move_impact

    # Privacy policy (#1280): only the WRITE side is gated. A speaker leaving a profile or
    # cluster is reconciled whatever the switch says (the old aggregate still averages its
    # voice, #1214); what is declined while voiceprints are off is folding a voice IN.
    file_id = db.query(Speaker.media_file_id).filter(Speaker.id == speaker_id).scalar()
    voiceprints_on = file_id is not None and PrivacyView(db).voiceprints(int(file_id)).enabled

    if old_profile_id == new_profile_id:
        if not (display_name_changed and new_profile_id):
            return []
        if not voiceprints_on:
            logger.info(
                "Profile rename refresh for speaker %s declined by privacy policy", speaker_id
            )
            return []
        impact = SpeakerImpact(profile_ids={new_profile_id})
    else:
        impact = profile_move_impact(
            db,
            speaker_id,
            old_profile_id,
            new_profile_id,
            voiceprints_enabled=voiceprints_on,
        )
    return reconcile_speaker_impact(db, impact)


def _update_opensearch_speaker_name(speaker_uuid: str, display_name: str) -> None:
    """Update speaker display name in OpenSearch."""
    try:
        update_speaker_display_name(speaker_uuid, display_name)
    except Exception as e:
        logger.exception(f"Failed to update speaker display name in OpenSearch: {e}")


def _handle_speaker_labeling_workflow(
    db: Session, speaker_id: int, display_name: str, user_id: int | None = None
) -> dict[str, int]:
    """
    Handle auto-creation of profiles and retroactive matching when speaker is labeled.

    Takes ``speaker_id`` rather than a ``Speaker``: the caller's read phase has
    already closed, so the row is re-read here against the session that will write
    it. An instance carried over from a closed session would lazy-load — silently
    opening a second transaction — on the first attribute the callees touch.

    Returns:
        dict with 'auto_applied_count' and 'suggested_count' keys
    """
    # Auto-create profile if needed and assign speaker to it
    from app.api.endpoints.speaker_update import auto_create_or_assign_profile

    speaker = db.query(Speaker).filter(Speaker.id == speaker_id).first()
    if not speaker:
        logger.warning(f"Speaker {speaker_id} disappeared before retroactive matching")
        return {"auto_applied_count": 0, "suggested_count": 0}

    auto_create_or_assign_profile(speaker, display_name, db, user_id=user_id)

    # Commit profile changes before retroactive matching
    db.commit()
    db.refresh(speaker)

    # Then trigger retroactive matching for all other speakers
    from app.api.endpoints.speaker_update import trigger_retroactive_matching

    return trigger_retroactive_matching(speaker, db)


def _clear_video_cache_for_speaker(media_file_id: int) -> None:
    """Clear video cache since speaker labels have changed (affects subtitles).

    **Takes no caller-owned ``Session``.** This runs from
    ``process_speaker_update_background``, which used to hold ONE transaction across
    its whole body — including the storage client construction and the five object
    deletes underneath. It now opens its own session, and only for the single
    filename SELECT the deletes need; the storage client is
    built before that session exists.

    No residual: the filename is resolved in a short session that CLOSES before any
    object is touched, and the deletes go through ``clear_derived_cache``, which takes
    no session at all. The session-taking ``clear_cache_for_media_file`` it used to call
    has been deleted outright.
    """
    try:
        from app.db.session_utils import session_scope
        from app.models.media import MediaFile
        from app.services.minio_service import MinIOService
        from app.services.video_processing_service import VideoProcessingService

        minio_service = MinIOService()
        video_processing_service = VideoProcessingService(minio_service)
        # Read phase: the filename, and nothing else. The scope closes before the
        # five MinIO deletes below, so no transaction spans them.
        with session_scope() as db:
            row = db.query(MediaFile.filename).filter(MediaFile.id == media_file_id).first()
        if row is None:
            logger.warning(f"Media file {media_file_id} not found for cache clearing")
            return
        video_processing_service.clear_derived_cache(media_file_id, str(row[0]))
    except Exception as e:
        logger.exception(f"Warning: Failed to clear video cache after speaker update: {e}")


def _handle_update_profile_action(
    profile_id: int,
    new_name: str,
    current_user: User,
    db: Session,
    organization_id: int | None = None,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> list[tuple[str, str]] | None:
    """Handle 'update_profile' action - update profile name globally.

    Postgres only. The OpenSearch fan-out this used to do inline — one
    ``update_speaker_display_name`` round trip per linked speaker plus a full
    ``ProfileEmbeddingService.update_profile_embedding`` (one kNN fetch per profile
    member, a numpy mean, and an index write) — is deferred to
    ``process_speaker_update_background`` (issue #284 A2.6). Renaming a profile with
    a dozen linked speakers used to cost a dozen-plus synchronous OpenSearch calls
    before the PUT could answer.

    The member rewrite and the ``(file_uuid, old_name)`` capture live in
    ``services/speaker_profile_rename.apply_profile_name_to_speakers`` — the ONE
    implementation of "a profile rename re-applies the name to its members", shared
    with ``PUT /speaker-profiles/profiles/{uuid}``, the other endpoint that renames a
    profile (issue #675). It collects the old names **before** the overwrite, because
    a profile rename spans files and that is the last moment the previous names exist
    anywhere: once committed, nothing in Postgres can say what the chunk plane was
    indexed with (issue #405).

    Returns:
        ``None`` when no such profile exists. Otherwise the renames to replay into
        the chunk index — an empty list when the profile had no linked speakers.
        A non-``None`` return also tells the caller a profile was renamed, so it can
        ask the background task to replay the rename into the speaker index.
    """
    from app.services.speaker_profile_rename import apply_profile_name_to_speakers

    # ``organization_id`` is the tenant of the speaker's file: only a profile of
    # that tenant is renamed through it.
    profile = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.id == profile_id,
            SpeakerProfile.user_id == current_user.id,
            org_scope_pred(SpeakerProfile.organization_id, organization_id),
        )
        .first()
    )

    if not profile:
        return None

    profile.name = new_name  # type: ignore[assignment]
    logger.info(f"Updated profile {profile.id} name to '{new_name}' globally")

    return apply_profile_name_to_speakers(
        db,
        profile_id,
        new_name,
        restrict_to_user_id=None if bypass.sees_all_in_scope else current_user.id,
        organization_id=organization_id,
    )


def _load_profile_speaker_names(db: Session, profile_id: int) -> list[tuple[str, str]]:
    """Read ``(speaker_uuid, display_name)`` for every speaker linked to a profile.

    Plain data only — no ORM instances — so the caller can close its session before
    pushing the names to the search index. Speakers with no display name are skipped
    here rather than in the push loop, so the returned length is the number of
    documents that will actually be written.
    """
    rows = (
        db.query(Speaker.uuid, Speaker.display_name).filter(Speaker.profile_id == profile_id).all()
    )
    return [(str(uuid_), str(name)) for uuid_, name in rows if name]


def _push_speaker_display_names(rows: list[tuple[str, str]]) -> None:
    """Write ``(speaker_uuid, display_name)`` pairs to the search index.

    **Takes no ``Session``.** One round trip per linked speaker — a dozen for a
    well-used profile — so this must never run with a transaction open. Callers do
    the read in a short session, close it, then call this.
    """
    for speaker_uuid, display_name in rows:
        _update_opensearch_speaker_name(speaker_uuid, display_name)
    logger.info(f"Synced renamed profile to OpenSearch for {len(rows)} speakers")


def _handle_create_new_profile_action(
    speaker: Speaker, new_name: str, current_user: User, db: Session
) -> None:
    """Handle 'create_new_profile' action - create new profile and assign speaker."""
    new_profile = SpeakerProfile(
        user_id=current_user.id,
        name=new_name,
        description=f"Profile for {new_name}",
        uuid=str(uuid.uuid4()),
        # Tenant stamp (issue #262e): the profile groups THIS speaker's voice,
        # so it inherits the org of the speaker's file; None = personal, always
        # None in the community edition.
        organization_id=speaker.media_file.organization_id,
    )
    db.add(new_profile)
    db.flush()  # Get the ID

    speaker.profile_id = new_profile.id
    record_human_identity(speaker, user_id=current_user.id, origin=ORIGIN_PROFILE_CREATED)
    logger.info(
        f"Created new profile {new_profile.id} '{new_name}' and assigned speaker {speaker.id}"
    )


def _handle_profile_action(
    profile_action: str | None,
    speaker_update: SpeakerUpdate,
    speaker: Speaker,
    old_profile_id: int | None,
    current_user: User,
    db: Session,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> tuple[int | None, list[tuple[str, str]]]:
    """Handle profile actions (update_profile or create_new_profile).

    Returns:
        ``(renamed_profile_id, chunk_renames)`` — the id of a profile that was
        renamed in place, so the caller can hand the OpenSearch replay to the
        background task (``None`` otherwise), and the ``(file_uuid, old_name)``
        pairs whose chunk documents still carry a pre-rename speaker name.
    """
    if not (profile_action and speaker_update.display_name):
        return None, []

    new_name = speaker_update.display_name.strip()

    if profile_action == "update_profile" and old_profile_id:
        renames = _handle_update_profile_action(
            old_profile_id,
            new_name,
            current_user,
            db,
            speaker.media_file.organization_id,
            bypass=bypass,
        )
        if renames is not None:
            return old_profile_id, renames
    elif profile_action == "create_new_profile":
        _handle_create_new_profile_action(speaker, new_name, current_user, db)
    return None, []


# Not the same helper as ``speaker_update._get_profile_uuid(db, profile_id)``: this one takes a
# Speaker and short-circuits on the already-loaded ``speaker.profile`` relationship before querying.
def _get_speaker_profile_uuid(speaker: Speaker, db: Session) -> str | None:
    """Get profile UUID from speaker, fetching from DB if necessary."""
    if not speaker.profile_id:
        return None

    if speaker.profile:
        return str(speaker.profile.uuid)

    profile = db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker.profile_id).first()
    return str(profile.uuid) if profile else None


def _load_speaker_profile_sync_payload(
    db: Session, speaker: Speaker, old_profile_id: int | None, display_name_changed: bool
) -> dict[str, Any] | None:
    """Resolve the profile fields a speaker's search document needs, as plain data.

    Returns ``None`` when nothing changed and no write is due — the same early
    return the pre-split ``_update_opensearch_profile_info`` made. Everything the
    write needs (including the profile UUID, which may require a second SELECT) is
    materialised here so the caller can close its session before writing.
    """
    new_profile_id = int(speaker.profile_id) if speaker.profile_id else None

    if old_profile_id == new_profile_id and not display_name_changed:
        return None

    return {
        "speaker_uuid": str(speaker.uuid),
        "profile_id": new_profile_id,
        "profile_uuid": _get_speaker_profile_uuid(speaker, db),
        "verified": bool(speaker.verified),
    }


def _push_speaker_profile_info(payload: dict[str, Any] | None) -> None:
    """Write a speaker's profile fields to the search index.

    **Takes no ``Session``.** ``None`` means the read phase decided there was
    nothing to sync.
    """
    if payload is None:
        return

    from app.services.opensearch_service import update_speaker_profile

    update_speaker_profile(**payload)


def _get_media_file_uuid(speaker: Speaker, db: Session) -> str | None:
    """Get media file UUID from speaker."""
    if speaker.media_file:
        return str(speaker.media_file.uuid)

    if speaker.media_file_id:
        row = db.query(MediaFile.uuid).filter(MediaFile.id == speaker.media_file_id).first()
        if row:
            return str(row[0])

    return None
