"""
Profile assignment when a speaker is labeled.

Split out of ``speaker_update``: creates or reuses the same-named profile of the
speaker's tenant, records who made the identity, and rebuilds the embeddings of the
profiles the speaker moved between. Retroactive matching (``speaker_update``) calls
``_update_profile_embedding`` when it auto-applies a label.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.services.privacy.redact_view import PrivacyView
from app.utils.db_helpers import org_stamp_is
from app.utils.speaker_identity_detail import ORIGIN_PROFILE_ASSIGNED
from app.utils.speaker_identity_detail import ORIGIN_PROFILE_CREATED
from app.utils.speaker_identity_source import record_human_identity

logger = logging.getLogger(__name__)


def auto_create_or_assign_profile(
    speaker: Speaker, display_name: str, db: Session, user_id: int | None = None
) -> bool:
    """
    Automatically create or assign speaker to a profile when labeled.

    This function implements the core auto-profiling logic:
    1. Searches for existing profiles with the same name (case-insensitive)
    2. If found, assigns speaker to existing profile and updates embedding
    3. If not found, creates new profile and assigns speaker
    4. Updates the profile's consolidated voice embedding

    Args:
        speaker (Speaker): The speaker instance to assign to a profile.
        display_name (str): The display name/label for the speaker.
        db (Session): SQLAlchemy database session.
        user_id: The person who typed the name, recorded as who made the identity.

    Returns:
        bool: Always True; a failure is raised, never returned as False.
    """
    speaker_id = speaker.id  # read before the try: a failed session cannot refresh it
    try:
        # The tenant of the speaker's file: a voiceprint only ever joins a profile
        # of the same tenant (None = personal; always None in the community edition).
        speaker_org_id = (
            db.query(MediaFile.organization_id)
            .filter(MediaFile.id == speaker.media_file_id)
            .scalar()
        )

        # The same-named profile of this user in the speaker's tenant, if any. Names
        # are unique per user per tenant (v430), so another tenant's profile of that
        # name is neither linked nor in the way of creating one here.
        existing_profile = (
            db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.user_id == speaker.user_id,
                SpeakerProfile.name.ilike(display_name.strip()),
                org_stamp_is(SpeakerProfile.organization_id, speaker_org_id),
            )
            .first()
        )

        old_profile_id = int(speaker.profile_id) if speaker.profile_id else None
        if existing_profile:
            # The user typed this name on this one speaker: that overrides an earlier
            # unassignment from the same profile (issue #1233).
            from app.services.speaker_identity_rules import lift_profile_blacklist

            lift_profile_blacklist(db, int(speaker.id), int(existing_profile.id))
            # Assign speaker to existing profile
            speaker.profile_id = existing_profile.id
            # A person typed this name (issue #1280). The request that saved the name already
            # recorded it, with the suggestion it accepted; this step must not overwrite that.
            record_human_identity(
                speaker, user_id=user_id, origin=ORIGIN_PROFILE_ASSIGNED, keep_recorded=True
            )
            logger.info(
                f"Assigned speaker {speaker.id} to existing profile '{existing_profile.name}' (ID: {existing_profile.id})"
            )

            _update_profile_embedding(db, int(speaker.id), old_profile_id, int(existing_profile.id))

            # Sync profile assignment to OpenSearch
            try:
                from app.services.opensearch_service import update_speaker_profile

                # Get profile UUID if profile is assigned
                profile_uuid = None
                if speaker.profile_id and existing_profile:
                    profile_uuid = str(existing_profile.uuid)

                update_speaker_profile(
                    speaker_uuid=str(speaker.uuid),
                    profile_id=int(speaker.profile_id) if speaker.profile_id else None,
                    profile_uuid=profile_uuid,
                    verified=bool(speaker.verified),
                )
                logger.info(f"Synced speaker {speaker.uuid} profile assignment to OpenSearch")
            except Exception as e:
                logger.warning(f"Failed to sync speaker {speaker.uuid} profile to OpenSearch: {e}")

        else:
            # Create new profile for this speaker name (org-stamped from the
            # speaker's tenant — issue #262e; None = personal/community).
            new_profile = SpeakerProfile(
                user_id=speaker.user_id,
                name=display_name.strip(),
                description=f"Auto-created profile for {display_name.strip()}",
                uuid=str(uuid.uuid4()),
                organization_id=speaker_org_id,
            )
            db.add(new_profile)
            db.flush()  # Get the ID without committing

            # Assign speaker to new profile
            speaker.profile_id = new_profile.id
            record_human_identity(
                speaker, user_id=user_id, origin=ORIGIN_PROFILE_CREATED, keep_recorded=True
            )
            logger.info(
                f"Created new profile '{new_profile.name}' (ID: {new_profile.id}) and assigned speaker {speaker.id}"
            )

            _update_profile_embedding(db, int(speaker.id), old_profile_id, int(new_profile.id))

            # Sync new profile assignment to OpenSearch
            try:
                from app.services.opensearch_service import update_speaker_profile

                update_speaker_profile(
                    speaker_uuid=str(speaker.uuid),
                    profile_id=int(speaker.profile_id) if speaker.profile_id else None,
                    profile_uuid=str(new_profile.uuid),
                    verified=bool(speaker.verified),
                )
                logger.info(f"Synced speaker {speaker.uuid} new profile assignment to OpenSearch")
            except Exception as e:
                logger.warning(
                    f"Failed to sync speaker {speaker.uuid} new profile to OpenSearch: {e}"
                )

        return True

    except Exception:
        logger.exception(f"Error in auto profile creation/assignment for speaker {speaker_id}")
        raise


def _update_profile_embedding(
    db: Session, speaker_id: int, old_profile_id: int | None, profile_id: int
) -> None:
    """Rebuild the profiles a speaker moved between, and the clusters it belongs to.

    The new profile gains the voice; a profile the speaker had before loses it, which
    used to be skipped here, so the old profile kept averaging a voice that had been
    reassigned away from it (issue #1214). Delegates to ``speaker_cleanup``, which logs
    any residual at ERROR; the label that triggered this has already committed.
    """
    from app.services.speaker_cleanup import profile_move_impact
    from app.services.speaker_cleanup import reconcile_speaker_impact

    # Privacy policy (#1280): only the WRITE side is gated. The profile the speaker left is
    # reconciled whatever the switch says (its aggregate still averages the voice, #1214);
    # folding the voice into the new profile is declined while voiceprints are off.
    file_id = db.query(Speaker.media_file_id).filter(Speaker.id == speaker_id).scalar()
    voiceprints_on = file_id is not None and PrivacyView(db).voiceprints(int(file_id)).enabled

    # Both callers set ``profile_id`` a moment ago and autoflush is off: without the flush
    # the rebuild reads the membership from before the link, and the profile embedding
    # never contains the voice that just joined it (issue #1233).
    db.flush()
    impact = profile_move_impact(
        db, speaker_id, old_profile_id, profile_id, voiceprints_enabled=voiceprints_on
    )
    reconcile_speaker_impact(db, impact)
