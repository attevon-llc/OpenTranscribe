import logging
import uuid
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile
from fastapi import status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCollection
from app.models.media import SpeakerCollectionMember
from app.models.media import SpeakerProfile
from app.models.user import User
from app.services.opensearch_service import update_speaker_collections
from app.services.permission_service import PermissionService
from app.services.speaker_embedding_service import SpeakerEmbeddingService
from app.services.speaker_matching_service import ConfidenceLevel
from app.services.speaker_matching_service import SpeakerMatchingService
from app.utils.error_handlers import ErrorHandler
from app.utils.uuid_helpers import get_speaker_by_uuid
from app.utils.uuid_helpers import get_speaker_profile_by_uuid
from app.utils.uuid_helpers import require_resource_owner

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/profiles", response_model=list[dict[str, Any]])
def list_speaker_profiles(
    collection_uuid: str | None = Query(None, description="Filter by collection UUID"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List all speaker profiles for the current user, including shared profiles."""
    try:
        # Admins see all profiles; regular users see own + shared
        is_admin = current_user.is_admin
        if is_admin:
            query = db.query(SpeakerProfile)
            owned_ids: set[int] = set()  # Will compute below for is_shared flag
        else:
            accessible = PermissionService.get_accessible_profile_ids_with_source(
                db, current_user.id
            )
            if not accessible:
                return []
            accessible_ids = [pid for pid, _ in accessible]
            owned_ids = {pid for pid, is_own in accessible if is_own}
            query = db.query(SpeakerProfile).filter(SpeakerProfile.id.in_(accessible_ids))

        if collection_uuid:
            # Filter by collection - convert UUID to ID
            from app.utils.uuid_helpers import get_by_uuid

            collection = get_by_uuid(db, SpeakerCollection, collection_uuid)
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

        # For admin, compute owned_ids to mark is_shared correctly
        if is_admin:
            owned_ids = {
                row[0]
                for row in db.query(SpeakerProfile.id)
                .filter(SpeakerProfile.user_id == current_user.id)
                .all()
            }

        # Pre-fetch owner names for shared profiles
        owner_user_ids = {int(p.user_id) for p in profiles if int(p.user_id) != current_user.id}
        owner_names: dict[int, str] = {}
        if owner_user_ids:
            owners = db.query(User).filter(User.id.in_(owner_user_ids)).all()
            for owner in owners:
                owner_names[owner.id] = owner.full_name or owner.email or f"User {owner.id}"

        from sqlalchemy import func as sa_func

        profile_ids = [p.id for p in profiles]

        # Batch query: media count and instance count per profile (1 query)
        count_rows = (
            db.query(
                Speaker.profile_id,
                sa_func.count(sa_func.distinct(Speaker.media_file_id)).label("media_count"),
                sa_func.count(Speaker.id).label("instance_count"),
            )
            .filter(Speaker.profile_id.in_(profile_ids))
            .group_by(Speaker.profile_id)
            .all()
        )
        counts_by_profile: dict[int, tuple[int, int]] = {
            row.profile_id: (row.media_count, row.instance_count) for row in count_rows
        }

        # Batch query: most common gender per profile using window function (1 query)
        gender_subq = (
            db.query(
                Speaker.profile_id,
                Speaker.predicted_gender,
                sa_func.count().label("cnt"),
                sa_func.row_number()
                .over(
                    partition_by=Speaker.profile_id,
                    order_by=sa_func.count().desc(),
                )
                .label("rn"),
            )
            .filter(
                Speaker.profile_id.in_(profile_ids),
                Speaker.predicted_gender.isnot(None),
            )
            .group_by(Speaker.profile_id, Speaker.predicted_gender)
            .subquery()
        )
        gender_rows = (
            db.query(gender_subq.c.profile_id, gender_subq.c.predicted_gender)
            .filter(gender_subq.c.rn == 1)
            .all()
        )
        gender_by_profile: dict[int, str] = {row[0]: row[1] for row in gender_rows}

        # Batch avatar URLs
        from app.services.minio_service import get_file_url

        result = []
        for profile in profiles:
            # created_at/updated_at have server_default=now() (non-None on persisted rows)
            assert profile.created_at is not None
            assert profile.updated_at is not None
            profile_id = profile.id
            is_shared = profile_id not in owned_ids
            media_count, instance_count = counts_by_profile.get(profile_id, (0, 0))

            avatar_url = None
            if profile.avatar_path:
                try:
                    avatar_url = get_file_url(profile.avatar_path, expires=3600)
                except Exception:
                    logger.warning(f"Failed to get avatar URL for profile {profile.uuid}")

            result.append(
                {
                    "uuid": str(profile.uuid),
                    "name": profile.name,
                    "description": profile.description,
                    "created_at": profile.created_at.isoformat(),
                    "updated_at": profile.updated_at.isoformat(),
                    "instance_count": instance_count,
                    "media_count": media_count,
                    "predicted_gender": gender_by_profile.get(profile_id),
                    "avatar_url": avatar_url,
                    "is_shared": is_shared,
                    "owner_name": owner_names.get(int(profile.user_id)) if is_shared else None,
                }
            )

        return result

    except HTTPException:
        # Intentional auth/validation errors (e.g. the foreign-collection 403 at
        # :65) must propagate unchanged — do not mask them as a generic 500.
        raise
    except Exception as e:
        logger.error(f"Error listing speaker profiles: {e}")
        raise ErrorHandler.internal_error() from e


@router.post("/profiles", response_model=dict[str, Any])
def create_speaker_profile(
    name: str,
    description: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Create a new speaker profile (org-stamped from the request context, #262e)."""
    try:
        # Check if profile with same name exists
        existing = (
            db.query(SpeakerProfile)
            .filter(SpeakerProfile.user_id == current_user.id, SpeakerProfile.name == name)
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

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating speaker profile: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


@router.put("/profiles/{profile_uuid}", response_model=dict[str, Any])
def update_speaker_profile(
    profile_uuid: str,
    name: str | None = None,
    description: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update a speaker profile."""
    try:
        profile = get_speaker_profile_by_uuid(db, profile_uuid)

        require_resource_owner(
            profile, current_user, forbidden_detail="Not authorized to access this profile"
        )

        profile_id = profile.id

        if name:
            # Check for name conflicts
            existing = (
                db.query(SpeakerProfile)
                .filter(
                    SpeakerProfile.user_id == current_user.id,
                    SpeakerProfile.name == name,
                    SpeakerProfile.id != profile_id,
                )
                .first()
            )

            if existing:
                raise HTTPException(
                    status_code=400,
                    detail="Speaker profile with this name already exists",
                )

            profile.name = name  # type: ignore[assignment]

        if description is not None:
            profile.description = description  # type: ignore[assignment]

        db.commit()
        db.refresh(profile)

        # updated_at populated by server_default after refresh
        assert profile.updated_at is not None
        return {
            "uuid": str(profile.uuid),
            "name": profile.name,
            "description": profile.description,
            "updated_at": profile.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating speaker profile: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


@router.post("/speakers/{speaker_uuid}/assign-profile", response_model=dict[str, Any])
def assign_speaker_to_profile(
    speaker_uuid: str,
    profile_uuid: str,
    confidence: float | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Assign a speaker instance to a profile (own or shared)."""
    try:
        # Verify speaker exists and user has file-level access
        speaker = get_speaker_by_uuid(db, speaker_uuid)
        file_perm = (
            "owner"
            if current_user.is_admin
            else PermissionService.get_file_permission(
                db, int(speaker.media_file_id), current_user.id
            )
        )
        if not file_perm:
            raise HTTPException(status_code=403, detail="Not authorized to access this speaker")
        speaker_id = speaker.id

        # Verify profile exists and is accessible (own or shared)
        profile = get_speaker_profile_by_uuid(db, profile_uuid)
        accessible_ids = PermissionService.get_accessible_profile_ids(db, current_user.id)
        if profile.id not in accessible_ids:
            raise HTTPException(status_code=403, detail="Not authorized to access this profile")
        profile_id = profile.id

        # Initialize services
        embedding_service = SpeakerEmbeddingService()
        matching_service = SpeakerMatchingService(db, embedding_service)

        # Assign speaker to profile
        updated_speaker = matching_service.assign_speaker_to_profile(
            int(speaker_id), int(profile_id), confidence
        )

        # Update collections in OpenSearch
        # For now, we'll use a default collection (could be expanded)
        update_speaker_collections(str(speaker.uuid), int(profile_id), str(profile.uuid), [])

        db.commit()

        return {
            "speaker_id": str(speaker.uuid),
            "profile_id": str(profile.uuid),
            "profile_name": profile.name,
            "confidence": confidence,
            "verified": updated_speaker.verified,
            "status": "assigned",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error assigning speaker to profile: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


def _get_embedding_suggestions(
    db: Session,
    speaker_id: int,
    current_user: User,
    threshold: float,
    accessible_profile_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Get profile suggestions based on voice embeddings."""
    from app.core.constants import SPEAKER_CONFIDENCE_HIGH
    from app.services.opensearch_service import get_speaker_embedding
    from app.services.profile_embedding_service import ProfileEmbeddingService
    from app.services.speaker_matching_service import SpeakerMatchingService

    suggestions: list[dict[str, Any]] = []

    # Get speaker object to extract UUID
    from app.models.media import Speaker

    speaker = db.query(Speaker).filter(Speaker.id == speaker_id).first()
    if not speaker:
        return suggestions

    speaker_embedding = get_speaker_embedding(str(speaker.uuid))
    if speaker_embedding:
        profile_matches = ProfileEmbeddingService.calculate_profile_similarity(
            db,
            speaker_embedding,
            current_user.id,
            threshold=threshold,
            accessible_profile_ids=accessible_profile_ids,
        )

        for match in profile_matches:
            confidence = match["similarity"]
            matching_service = SpeakerMatchingService(db, None)  # type: ignore[arg-type]
            confidence_level = matching_service.get_confidence_level(float(confidence))
            auto_accept = confidence >= SPEAKER_CONFIDENCE_HIGH

            # Generate reason based on confidence
            if confidence >= 0.9:
                reason = f"Very strong voice match (based on {match['embedding_count']} recordings)"
            elif confidence >= 0.8:
                reason = f"Strong voice match (based on {match['embedding_count']} recordings)"
            elif confidence >= 0.7:
                reason = f"Good voice match (based on {match['embedding_count']} recordings)"
            else:
                reason = f"Possible voice match (based on {match['embedding_count']} recordings)"

            suggestions.append(
                {
                    "profile_id": match["profile_uuid"],  # Return UUID for external API
                    "profile_name": match["profile_name"],
                    "confidence": confidence,
                    "confidence_level": confidence_level,
                    "auto_accept": auto_accept,
                    "reason": reason,
                    "source": "voice_embedding",
                    "embedding_count": match["embedding_count"],
                    "last_update": match.get("last_update"),
                }
            )
    else:
        logger.warning(f"No embedding found for speaker {speaker_id}")

    return suggestions


def _get_llm_suggestions(
    db: Session,
    speaker: Speaker,
    current_user: User,
    accessible_profile_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Get profile suggestions based on LLM analysis."""
    from app.services.speaker_matching_service import SpeakerMatchingService

    suggestions: list[dict[str, Any]] = []
    if (
        speaker.suggested_name
        and speaker.confidence
        and speaker.suggestion_source == "llm_analysis"
    ):
        # Check if suggested_name matches any accessible profiles
        profile_query = db.query(SpeakerProfile).filter(
            SpeakerProfile.name.ilike(f"%{speaker.suggested_name}%"),
        )
        if accessible_profile_ids is not None:
            profile_query = profile_query.filter(SpeakerProfile.id.in_(accessible_profile_ids))
        else:
            profile_query = profile_query.filter(SpeakerProfile.user_id == current_user.id)
        suggested_profile = profile_query.first()

        matching_service = SpeakerMatchingService(db, None)  # type: ignore[arg-type]
        confidence_level = matching_service.get_confidence_level(float(speaker.confidence))

        if suggested_profile:
            # Add LLM suggestion for existing profile
            suggestions.append(
                {
                    "profile_id": str(suggested_profile.uuid),
                    "profile_name": str(suggested_profile.name),
                    "confidence": float(speaker.confidence),
                    "confidence_level": confidence_level,
                    "auto_accept": bool(speaker.confidence >= 0.8),
                    "reason": f"AI content analysis suggests this speaker is '{speaker.suggested_name}'",
                    "source": "llm_analysis",
                    "suggested_name": str(speaker.suggested_name),
                }
            )
        else:
            # Add LLM suggestion for new profile creation
            suggestions.append(
                {
                    "profile_id": None,
                    "profile_name": str(speaker.suggested_name),
                    "confidence": float(speaker.confidence),
                    "confidence_level": confidence_level,
                    "auto_accept": False,
                    "reason": f"AI content analysis suggests creating new profile for '{speaker.suggested_name}'",
                    "source": "llm_analysis",
                    "suggested_name": str(speaker.suggested_name),
                    "create_new": True,
                }
            )

    return suggestions


@router.get("/speakers/{speaker_uuid}/suggestions", response_model=list[dict[str, Any]])
def get_speaker_profile_suggestions(
    speaker_uuid: str,
    threshold: float = Query(ConfidenceLevel.MEDIUM, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get profile suggestions for a speaker based on both embeddings and LLM analysis."""
    try:
        # Verify speaker exists and user has file-level access
        speaker = get_speaker_by_uuid(db, speaker_uuid)
        file_perm = (
            "owner"
            if current_user.is_admin
            else PermissionService.get_file_permission(
                db, int(speaker.media_file_id), current_user.id
            )
        )
        if not file_perm:
            raise HTTPException(status_code=403, detail="Not authorized to access this speaker")
        speaker_id = speaker.id

        # Check if speaker already has a profile
        if speaker.profile_id:
            return []

        # Get media file for audio processing
        media_file = db.query(MediaFile).filter(MediaFile.id == speaker.media_file_id).first()
        if not media_file:
            return []

        # Compute accessible profiles for cross-user matching
        accessible_ids = PermissionService.get_accessible_profile_ids(db, current_user.id)

        # Get suggestions from different sources
        suggestions: list[dict[str, Any]] = []
        suggestions.extend(
            _get_embedding_suggestions(
                db,
                int(speaker_id),
                current_user,
                threshold,
                accessible_profile_ids=accessible_ids,
            )
        )
        suggestions.extend(
            _get_llm_suggestions(db, speaker, current_user, accessible_profile_ids=accessible_ids)
        )

        # Sort suggestions by confidence (highest first) and source priority
        def sort_key(suggestion):
            source_priority = 0 if suggestion["source"] == "voice_embedding" else 1
            return (source_priority, -suggestion["confidence"])

        suggestions.sort(key=sort_key)
        return suggestions

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting speaker suggestions: {e}")
        raise ErrorHandler.internal_error() from e


@router.get("/profiles/{profile_uuid}/occurrences", response_model=list[dict[str, Any]])
def get_speaker_profile_occurrences(
    profile_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get all media files where a speaker profile appears."""
    try:
        # Verify profile exists and is accessible (own or shared)
        profile = get_speaker_profile_by_uuid(db, profile_uuid)
        accessible_ids = PermissionService.get_accessible_profile_ids(db, current_user.id)
        if profile.id not in accessible_ids:
            raise HTTPException(status_code=403, detail="Not authorized to access this profile")
        profile_id = profile.id

        # find_speaker_occurrences is a pure DB read — never construct
        # SpeakerEmbeddingService here: it imports pyannote and loads the
        # embedding model, which the API/CI image doesn't ship (→ 500) and
        # which would waste model loads per request even where it does.
        matching_service = SpeakerMatchingService(db, None)

        # Get occurrences
        occurrences = matching_service.find_speaker_occurrences(int(profile_id), current_user.id)

        return occurrences

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting speaker occurrences: {e}")
        raise ErrorHandler.internal_error() from e


@router.delete("/profiles/{profile_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_speaker_profile(
    profile_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete a speaker profile."""
    try:
        profile = get_speaker_profile_by_uuid(db, profile_uuid)
        require_resource_owner(
            profile, current_user, forbidden_detail="Not authorized to access this profile"
        )
        profile_id = profile.id

        # Unassign all speakers from this profile
        speakers = db.query(Speaker).filter(Speaker.profile_id == profile_id).all()
        for speaker in speakers:
            speaker.profile_id = None  # type: ignore[assignment]
            speaker.verified = False  # type: ignore[assignment]

        # Delete avatar from MinIO if exists
        if profile.avatar_path:
            try:
                from app.services.minio_service import delete_file

                delete_file(profile.avatar_path)
            except Exception:
                logger.warning(f"Failed to delete avatar for profile {profile.uuid}")

        # Capture UUID before DB delete
        profile_uuid_str = str(profile.uuid)

        # Delete the profile
        db.delete(profile)
        db.commit()

        # Remove profile embedding from all OpenSearch indices (non-fatal)
        try:
            from app.services.opensearch_service import remove_profile_embedding

            remove_profile_embedding(profile_uuid_str)
        except Exception as e:
            logger.warning(f"Failed to remove profile {profile_uuid_str} from OpenSearch: {e}")

        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting speaker profile: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_AVATAR_SIZE = 2 * 1024 * 1024  # 2MB


@router.post("/profiles/{profile_uuid}/avatar", response_model=dict[str, Any])
async def upload_profile_avatar(
    profile_uuid: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Upload an avatar image for a speaker profile."""
    try:
        profile = get_speaker_profile_by_uuid(db, profile_uuid)
        require_resource_owner(
            profile, current_user, forbidden_detail="Not authorized to access this profile"
        )

        # Validate content type
        if file.content_type not in ALLOWED_AVATAR_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_AVATAR_TYPES)}",
            )

        # Read and validate size
        content = await file.read()
        if len(content) > MAX_AVATAR_SIZE:
            raise HTTPException(status_code=400, detail="File too large. Maximum size is 2MB.")

        # Determine extension from content type
        ext_map = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
        }
        ext = ext_map.get(file.content_type, "jpg")

        # Delete old avatar if exists
        if profile.avatar_path:
            try:
                from app.services.minio_service import delete_file

                await run_in_threadpool(delete_file, profile.avatar_path)
            except Exception:
                logger.warning(f"Failed to delete old avatar for profile {profile.uuid}")

        # Upload to MinIO
        import io

        from app.services.minio_service import get_file_url
        from app.services.minio_service import upload_file

        object_name = f"avatars/{current_user.id}/{profile.uuid}.{ext}"
        await run_in_threadpool(
            upload_file, io.BytesIO(content), len(content), object_name, file.content_type
        )

        # Update profile
        profile.avatar_path = object_name  # type: ignore[assignment]
        db.commit()
        db.refresh(profile)

        avatar_url = await run_in_threadpool(get_file_url, object_name, expires=3600)
        return {"uuid": str(profile.uuid), "avatar_url": avatar_url}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading avatar: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


@router.delete("/profiles/{profile_uuid}/avatar", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile_avatar(
    profile_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Remove a speaker profile's avatar."""
    try:
        profile = get_speaker_profile_by_uuid(db, profile_uuid)
        require_resource_owner(
            profile, current_user, forbidden_detail="Not authorized to access this profile"
        )

        if profile.avatar_path:
            try:
                from app.services.minio_service import delete_file

                delete_file(profile.avatar_path)
            except Exception:
                logger.warning(f"Failed to delete avatar file for profile {profile.uuid}")

            profile.avatar_path = None  # type: ignore[assignment]
            db.commit()

        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting avatar: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


@router.post("/profiles/{profile_uuid}/confirm-gender", response_model=dict[str, Any])
def confirm_profile_gender(
    profile_uuid: str,
    gender: str = Query(..., description="Gender value: 'male' or 'female'"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Confirm or set the predicted gender for a profile and all linked speakers."""
    if gender not in ("male", "female"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gender must be 'male' or 'female'",
        )

    profile = get_speaker_profile_by_uuid(db, profile_uuid)
    require_resource_owner(
        profile, current_user, forbidden_detail="Not authorized to access this profile"
    )

    # Update profile DB column for consistency
    profile.predicted_gender = gender  # type: ignore[assignment]

    # Bulk-update all linked speakers
    updated_count = (
        db.query(Speaker)
        .filter(Speaker.profile_id == profile.id)
        .update(
            {"predicted_gender": gender, "gender_confirmed_by_user": True},
            synchronize_session="fetch",
        )
    )

    db.commit()

    return {
        "profile_uuid": str(profile.uuid),
        "predicted_gender": gender,
        "updated_count": updated_count,
    }


@router.get("/collections", response_model=list[dict[str, Any]])
def list_speaker_collections(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)
):
    """List all speaker collections for the current user."""
    try:
        collections = (
            db.query(SpeakerCollection).filter(SpeakerCollection.user_id == current_user.id).all()
        )

        # Batch-fetch member counts (avoids N+1 per-collection queries)
        from sqlalchemy import func as sa_func

        collection_ids = [c.id for c in collections]
        count_rows = (
            db.query(
                SpeakerCollectionMember.collection_id,
                sa_func.count().label("cnt"),
            )
            .filter(SpeakerCollectionMember.collection_id.in_(collection_ids))
            .group_by(SpeakerCollectionMember.collection_id)
            .all()
            if collection_ids
            else []
        )
        member_counts = {r.collection_id: r.cnt for r in count_rows}

        result = []
        for collection in collections:
            # created_at/updated_at have server_default=now() (non-None on persisted rows)
            assert collection.created_at is not None
            assert collection.updated_at is not None
            result.append(
                {
                    "uuid": str(collection.uuid),
                    "name": collection.name,
                    "description": collection.description,
                    "is_public": collection.is_public,
                    "created_at": collection.created_at.isoformat(),
                    "updated_at": collection.updated_at.isoformat(),
                    "member_count": member_counts.get(collection.id, 0),
                }
            )

        return result

    except Exception as e:
        logger.error(f"Error listing speaker collections: {e}")
        raise ErrorHandler.internal_error() from e


@router.post("/collections", response_model=dict[str, Any])
def create_speaker_collection(
    name: str,
    description: str | None = None,
    is_public: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Create a new speaker collection."""
    try:
        # Check for name conflicts
        existing = (
            db.query(SpeakerCollection)
            .filter(
                SpeakerCollection.user_id == current_user.id,
                SpeakerCollection.name == name,
            )
            .first()
        )

        if existing:
            raise HTTPException(status_code=400, detail="Collection with this name already exists")

        collection = SpeakerCollection(
            user_id=current_user.id,
            name=name,
            description=description,
            is_public=is_public,
        )

        db.add(collection)
        db.commit()
        db.refresh(collection)

        # created_at/updated_at populated by server_default after refresh
        assert collection.created_at is not None
        assert collection.updated_at is not None
        return {
            "uuid": str(collection.uuid),
            "name": collection.name,
            "description": collection.description,
            "is_public": collection.is_public,
            "created_at": collection.created_at.isoformat(),
            "updated_at": collection.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating speaker collection: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e
