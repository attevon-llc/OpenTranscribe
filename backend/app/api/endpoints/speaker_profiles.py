import logging
import uuid
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints import speaker_profiles_attributes
from app.api.endpoints import speaker_profiles_collections
from app.api.endpoints import speaker_profiles_matching
from app.api.endpoints.auth import get_current_active_user
from app.api.error_handlers import ErrorHandler
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCollection
from app.models.speaker import SpeakerCollectionMember
from app.models.speaker import SpeakerProfile
from app.models.user import User
from app.services.permission_service import PermissionService
from app.services.permission_service import org_scope_pred
from app.services.platform_bypass import PlatformBypass
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import derived_profile_genders
from app.services.request_context import RequestContext
from app.services.speaker_matching_service import SpeakerMatchingService
from app.services.speaker_profile_rename import apply_profile_name_to_speakers
from app.services.uuid_helpers import _resource_in_tenant_scope
from app.services.uuid_helpers import require_profile_in_scope
from app.services.uuid_helpers import require_resource_owner
from app.utils.db_helpers import org_stamp_is

logger = logging.getLogger(__name__)

router = APIRouter()


def _owner_names_for_shared(
    db: Session, profiles: list[SpeakerProfile], current_user_id: int
) -> dict[int, str]:
    """Display names of the other users who own any of ``profiles`` (shared-profile labels)."""
    owner_user_ids = {int(p.user_id) for p in profiles if int(p.user_id) != current_user_id}
    owner_names: dict[int, str] = {}
    if owner_user_ids:
        owners = db.query(User).filter(User.id.in_(owner_user_ids)).all()
        for owner in owners:
            owner_names[owner.id] = owner.full_name or owner.email or f"User {owner.id}"
    return owner_names


def _profile_media_instance_counts(
    db: Session, profile_ids: list[int]
) -> dict[int, tuple[int, int]]:
    """Per-profile ``(media_count, instance_count)``, hiding voiceprint links the policy hides."""
    from sqlalchemy import func as sa_func

    # A link a voiceprint made is not counted while its file's voiceprints are off
    # (#1280): the profile would otherwise report appearances the speaker pages hide.
    # Decided per speaker by its FILE's scope; a person's own link is always counted.
    from sqlalchemy import select

    from app.services.privacy.redact_view import machine_identity_clauses

    _name_hidden, link_hidden = machine_identity_clauses(
        PrivacyView(db),
        select(Speaker.media_file_id).where(Speaker.profile_id.in_(profile_ids)),
    )

    # Batch query: media count and instance count per profile (1 query)
    count_rows = (
        db.query(
            Speaker.profile_id,
            sa_func.count(sa_func.distinct(Speaker.media_file_id)).label("media_count"),
            sa_func.count(Speaker.id).label("instance_count"),
        )
        .filter(Speaker.profile_id.in_(profile_ids), ~link_hidden)
        .group_by(Speaker.profile_id)
        .all()
    )
    counts_by_profile: dict[int, tuple[int, int]] = {
        row.profile_id: (row.media_count, row.instance_count) for row in count_rows
    }

    return counts_by_profile


def _profile_avatar_url(profile: SpeakerProfile, bypass: PlatformBypass) -> str | None:
    """Presigned avatar URL for ``profile``, or ``None`` when absent or unavailable."""
    from app.services.minio_service import get_file_url

    avatar_url = None
    if profile.avatar_path:
        try:
            avatar_url = get_file_url(profile.avatar_path, expires=bypass.presign_ttl(3600))
        except Exception:
            logger.warning(f"Failed to get avatar URL for profile {profile.uuid}")
    return avatar_url


def _profile_list_entry(
    profile: SpeakerProfile,
    *,
    instance_count: int,
    media_count: int,
    predicted_gender: str | None,
    avatar_url: str | None,
    is_shared: bool,
    owner_name: str | None,
) -> dict[str, Any]:
    """One row of the profile list response."""
    return {
        "uuid": str(profile.uuid),
        "name": profile.name,
        "description": profile.description,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
        "instance_count": instance_count,
        "media_count": media_count,
        # A CONFIRMED gender wins over the derived majority (#543).
        #
        # `SpeakerProfile.predicted_gender` had exactly one writer
        # (`confirm_profile_gender`) and ZERO readers, so confirming a
        # gender persisted a column nothing ever looked at. On a profile
        # WITH members it appeared to work only because that same call
        # also bulk-sets every member, which moved the derived majority;
        # on a member-less profile the write was invisible everywhere and
        # the UI toggle could never light up.
        #
        # Behaviour-preserving for every profile that has ever been
        # confirmed: the confirm writes both, so the column and the
        # majority already agree (verified on live data). A profile that
        # was never confirmed has NULL here and still reports the derived
        # value — byte-identical to before.
        "predicted_gender": predicted_gender,
        "avatar_url": avatar_url,
        "is_shared": is_shared,
        "owner_name": owner_name,
    }


@router.get("/profiles", response_model=list[dict[str, Any]])
def list_speaker_profiles(
    collection_uuid: str | None = Query(None, description="Filter by collection UUID"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """List the caller's speaker profiles in the active tenant, including shared ones."""
    try:
        # A caller who sees all in scope gets every profile of the tenant; regular users
        # see own + shared
        sees_all = ctx.bypass.sees_all_in_scope
        if sees_all:
            query = db.query(SpeakerProfile)
            tenant_pred = org_scope_pred(SpeakerProfile.organization_id, ctx.org_id)
            if tenant_pred is not None:
                query = query.filter(tenant_pred)
            owned_ids: set[int] = set()  # Will compute below for is_shared flag
        else:
            accessible = PermissionService.get_accessible_profile_ids_with_source(
                db, current_user.id, organization_id=ctx.org_id
            )
            if not accessible:
                return []
            accessible_ids = [pid for pid, _ in accessible]
            owned_ids = {pid for pid, is_own in accessible if is_own}
            query = db.query(SpeakerProfile).filter(SpeakerProfile.id.in_(accessible_ids))

        if collection_uuid:
            # Filter by collection - convert UUID to ID
            from app.services.uuid_helpers import get_by_uuid

            collection = get_by_uuid(db, SpeakerCollection, collection_uuid)
            if not _resource_in_tenant_scope(collection, ctx.org_id):
                raise HTTPException(status_code=404, detail="SpeakerCollection not found")
            require_resource_owner(
                collection,
                current_user,
                forbidden_detail="Not authorized to access this collection",
            )
            collection_id = collection.id

            query = query.join(SpeakerCollectionMember).filter(
                SpeakerCollectionMember.collection_id == collection_id
            )

        profiles = query.all()

        # For a see-all caller, compute owned_ids to mark is_shared correctly
        if sees_all:
            owned_ids = {
                row[0]
                for row in db.query(SpeakerProfile.id)
                .filter(SpeakerProfile.user_id == current_user.id)
                .all()
            }

        owner_names = _owner_names_for_shared(db, profiles, current_user.id)
        profile_ids = [p.id for p in profiles]

        counts_by_profile = _profile_media_instance_counts(db, profile_ids)

        # Most common gender per profile, as the privacy policy allows it (#1280): this is
        # the DERIVED gender, a majority vote over the member speakers. A profile whose
        # owner scope has gender inference off is voted on by CONFIRMED genders only. It is
        # the fallback, not the answer: a gender the user explicitly CONFIRMED on the
        # profile wins (see `predicted_gender` below, issue #543).
        gender_by_profile = derived_profile_genders(db, profiles)

        result = []
        for profile in profiles:
            # created_at/updated_at are nullable: a server_default fills a column only for
            # an INSERT that omits it, so a raw-SQL insert, a backfill or an explicit UPDATE
            # leaves NULL on a persisted row. These are plain dict fields, so an unknown
            # timestamp is reported as null — asserting here 500s the caller's entire
            # profile list over one row, and vanishes under `python -O` anyway.
            profile_id = profile.id
            is_shared = profile_id not in owned_ids
            media_count, instance_count = counts_by_profile.get(profile_id, (0, 0))

            avatar_url = _profile_avatar_url(profile, ctx.bypass)

            result.append(
                _profile_list_entry(
                    profile,
                    instance_count=instance_count,
                    media_count=media_count,
                    predicted_gender=profile.predicted_gender or gender_by_profile.get(profile_id),
                    avatar_url=avatar_url,
                    is_shared=is_shared,
                    owner_name=owner_names.get(int(profile.user_id)) if is_shared else None,
                )
            )

        return result

    except (HTTPException, DomainError):
        # Intentional auth/validation errors (e.g. the foreign-collection 403 at
        # :65) must propagate unchanged — do not mask them as a generic 500.
        raise
    except Exception as e:
        logger.exception(f"Error listing speaker profiles: {e}")
        raise ErrorHandler.internal_error() from e


@router.post(
    "/profiles", response_model=dict[str, Any], dependencies=[Depends(refuse_under_support_grant)]
)
def create_speaker_profile(
    name: str,
    description: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Create a new speaker profile (org-stamped from the request context, #262e)."""
    try:
        # Names are unique per user per tenant (v430).
        existing = (
            db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.user_id == current_user.id,
                SpeakerProfile.name == name,
                org_stamp_is(SpeakerProfile.organization_id, ctx.org_id),
            )
            .first()
        )

        if existing:
            raise HTTPException(
                status_code=400, detail="Speaker profile with this name already exists"
            )

        profile = SpeakerProfile(
            user_id=current_user.id,
            name=name,
            description=description,
            uuid=str(uuid.uuid4()),
            # Tenant stamp — matches the pipeline-side create_speaker_profile
            # (None = personal scope; always None in the community edition).
            organization_id=ctx.org_id,
        )

        db.add(profile)
        db.commit()
        db.refresh(profile)

        # created_at/updated_at populated by server_default after refresh
        assert profile.created_at is not None
        assert profile.updated_at is not None
        return {
            "uuid": str(profile.uuid),
            "name": profile.name,
            "description": profile.description,
            "created_at": profile.created_at.isoformat(),
            "updated_at": profile.updated_at.isoformat(),
        }

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error creating speaker profile: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


def _dispatch_profile_rename_propagation(
    renames: list[tuple[str, str]], new_name: str | None
) -> None:
    """Replay a committed profile rename into ``transcript_chunks`` (issue #675).

    Chunk documents snapshot the speaker display name at index time, and nothing
    repairs a merely-*wrong* value: ``search_index_maintenance`` only finds files
    with no chunks at all. So a profile rename that skipped this left search facets,
    chat's speaker-scoped ``terms`` filter and every citation serving the old name
    until someone happened to reindex the file.

    ``dispatch_speaker_rename`` is the single entry point every rename path uses
    (``tasks/rename_propagation_task.py``); it coalesces the pairs per file, so one
    task is queued per affected file however many members that file holds.

    **No ``speaker_id``.** That argument exists so the task can re-resolve the
    current name at run time and converge when two renames race. A profile-wide
    rename sweeps many speakers at once and has no single id to re-read — the
    omission is the documented contract, not an oversight.

    Best-effort by design, and it must stay that way: the rename is already
    committed, so an unreachable broker must not turn it into a caller-visible
    failure. The chunk plane then stays stale until the next reindex, which is
    exactly the pre-#405 behaviour.
    """
    if not renames or not new_name:
        return
    try:
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        dispatch_speaker_rename(renames, new_name)
    except Exception as exc:  # noqa: BLE001 — a committed rename must not 500 on dispatch
        logger.warning(f"Could not queue chunk-plane profile rename propagation: {exc}")


@router.put("/profiles/{profile_uuid}", response_model=dict[str, Any])
def update_speaker_profile(
    profile_uuid: str,
    name: str | None = None,
    description: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Update a speaker profile.

    A name change is **not** a metadata edit. ``SpeakerProfile.name`` is not a
    field of any search document; what the chunk plane carries is each member
    speaker's canonical label, so renaming the profile re-applies the new name to
    every member (the invariant every other profile-linking path upholds — see
    ``services/speaker_profile_rename``) and then replays the change into
    ``transcript_chunks``. Doing only one half would leave Postgres and the index
    disagreeing in whichever direction was skipped.
    """
    try:
        profile = require_profile_in_scope(
            db, profile_uuid, current_user, organization_id=ctx.org_id
        )

        profile_id = profile.id
        renames: list[tuple[str, str]] = []

        if name:
            # Check for name conflicts
            existing = (
                db.query(SpeakerProfile)
                .filter(
                    SpeakerProfile.user_id == current_user.id,
                    SpeakerProfile.name == name,
                    SpeakerProfile.id != profile_id,
                    org_stamp_is(SpeakerProfile.organization_id, profile.organization_id),
                )
                .first()
            )

            if existing:
                raise HTTPException(
                    status_code=400,
                    detail="Speaker profile with this name already exists",
                )

            profile.name = name  # type: ignore[assignment]
            # Collected BEFORE the commit below: these are the names the chunk
            # documents were indexed with, and after the commit Postgres can no
            # longer say what they were (issue #405).
            renames = apply_profile_name_to_speakers(
                db,
                int(profile_id),
                name,
                restrict_to_user_id=None if ctx.bypass.sees_all_in_scope else current_user.id,
                organization_id=profile.organization_id,
            )

        if description is not None:
            profile.description = description  # type: ignore[assignment]

        db.commit()
        db.refresh(profile)

        # ─── POST-COMMIT REGION ───────────────────────────────────────────────
        # The rename is durable from here, so nothing below may raise a 500 that
        # tells the client it did not happen — the broad handler's `db.rollback()`
        # cannot undo it. Dispatch AFTER the commit, never before: a rolled-back
        # rename that reached the index would leave the new name existing only
        # there (`SpeakerRenameTracker.flush`'s reasoning, same rule).
        _dispatch_profile_rename_propagation(renames, name)

        # updated_at populated by server_default after refresh
        assert profile.updated_at is not None
        return {
            "uuid": str(profile.uuid),
            "name": profile.name,
            "description": profile.description,
            "updated_at": profile.updated_at.isoformat(),
        }

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error updating speaker profile: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


router.include_router(speaker_profiles_matching.router)


@router.get("/profiles/{profile_uuid}/occurrences", response_model=list[dict[str, Any]])
def get_speaker_profile_occurrences(
    profile_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get all media files of the active tenant where a speaker profile appears."""
    try:
        profile = require_profile_in_scope(
            db, profile_uuid, current_user, organization_id=ctx.org_id, owner_only=False
        )
        profile_id = profile.id

        # find_speaker_occurrences is a pure DB read — never construct
        # SpeakerEmbeddingService here: it imports pyannote and loads the
        # embedding model, which the API/CI image doesn't ship (→ 500) and
        # which would waste model loads per request even where it does.
        matching_service = SpeakerMatchingService(db, None)

        # Get occurrences. A quarantined file's occurrence must 404-equivalent drop out
        # of this list for a non-admin, the same as everywhere else under `files/`.
        occurrences = matching_service.find_speaker_occurrences(
            int(profile_id),
            current_user.id,
            include_quarantined=ctx.bypass.user_is_admin,
            organization_id=ctx.org_id,
        )

        return occurrences

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error getting speaker occurrences: {e}")
        raise ErrorHandler.internal_error() from e


@router.delete("/profiles/{profile_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_speaker_profile(
    profile_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Delete a speaker profile, taking its name and voice back from everything it reached.

    A linked speaker carries the profile's name as its display name (the invariant
    ``services/speaker_profile_rename`` documents), so unlinking alone left every member
    still labelled as the deleted identity. ``speaker_identity_moves.unlink_from_profile`` — the
    same rule a cluster unassign applies (#1233) — clears a display name or suggestion
    that IS the profile's name (the label falls back to the diarizer label, and the chunk
    plane is told), and a cluster named after the profile loses that label.
    The profile's embedding is erased and verified, and every cluster that held a member
    or was promoted to the profile is rebuilt by ``speaker_cleanup`` — an emptied
    promoted cluster is deleted with its centroid instead of being left listed with
    nobody in it (issue #1214). A display name the user set to something else survives.
    """
    from app.models.speaker import SpeakerCluster
    from app.services.speaker_cleanup import SpeakerCleanupIncompleteError
    from app.services.speaker_cleanup import capture_speaker_impact
    from app.services.speaker_cleanup import raise_if_incomplete
    from app.services.speaker_cleanup import reconcile_speaker_impact
    from app.services.speaker_identity_moves import unlink_from_profile
    from app.services.speaker_rename_tracker import SpeakerRenameTracker

    tracker = SpeakerRenameTracker()
    try:
        profile = require_profile_in_scope(
            db, profile_uuid, current_user, organization_id=ctx.org_id
        )
        profile_id = profile.id
        profile_name = str(profile.name)

        speakers = db.query(Speaker).filter(Speaker.profile_id == profile_id).all()
        impact = capture_speaker_impact(
            db, [int(s.id) for s in speakers], removed=False, reindex=False
        )
        impact.owner_ids.add(int(profile.user_id))
        promoted = (
            db.query(SpeakerCluster)
            .filter(SpeakerCluster.promoted_to_profile_id == profile_id)
            .all()
        )
        for cluster in promoted:
            impact.cluster_uuids.add(str(cluster.uuid))
            impact.owner_ids.add(int(cluster.user_id))
            if cluster.label == profile_name:
                cluster.label = None  # type: ignore[assignment]

        # The one unlink rule, shared with a cluster unassign (#1233).
        impact.relabel_speaker_ids |= unlink_from_profile(db, speakers, int(profile_id), tracker)

        # Delete avatar from MinIO if exists
        if profile.avatar_path:
            try:
                from app.services.minio_service import delete_file

                delete_file(profile.avatar_path)
            except Exception:
                logger.warning(f"Failed to delete avatar for profile {profile.uuid}")

        impact.erase_profile_uuids.add(str(profile.uuid))
        db.delete(profile)
        db.commit()
        tracker.flush(db)

        raise_if_incomplete(reconcile_speaker_impact(db, impact))
        return None

    except (HTTPException, SpeakerCleanupIncompleteError, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error deleting speaker profile: {e}")
        db.rollback()
        tracker.discard()
        raise ErrorHandler.internal_error() from e


router.include_router(speaker_profiles_attributes.router)
router.include_router(speaker_profiles_collections.router)
