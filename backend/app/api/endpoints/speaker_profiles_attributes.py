"""Speaker profile avatar and confirmed-gender endpoints (split from speaker_profiles)."""

import logging
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

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.error_handlers import ErrorHandler
from app.core.constants import MAX_AVATAR_SIZE
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.speaker import Speaker
from app.models.user import User
from app.services.permission_service import file_ids_in_scope
from app.services.request_context import RequestContext
from app.services.uuid_helpers import require_profile_in_scope

logger = logging.getLogger(__name__)

router = APIRouter()


ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


@router.post("/profiles/{profile_uuid}/avatar", response_model=dict[str, Any])
async def upload_profile_avatar(
    profile_uuid: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Upload an avatar image for a speaker profile."""
    try:
        profile = require_profile_in_scope(
            db, profile_uuid, current_user, organization_id=ctx.org_id
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

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error uploading avatar: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


@router.delete("/profiles/{profile_uuid}/avatar", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile_avatar(
    profile_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Remove a speaker profile's avatar."""
    try:
        profile = require_profile_in_scope(
            db, profile_uuid, current_user, organization_id=ctx.org_id
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

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error deleting avatar: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e


@router.post("/profiles/{profile_uuid}/confirm-gender", response_model=dict[str, Any])
def confirm_profile_gender(
    profile_uuid: str,
    gender: str = Query(..., description="Gender value: 'male' or 'female'"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Confirm the gender for a profile and the caller's own linked speakers.

    Only the caller's speakers in the active tenant are rewritten: other users'
    speakers linked to this profile (via a shared file) belong to them.
    """
    if gender not in ("male", "female"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gender must be 'male' or 'female'",
        )

    profile = require_profile_in_scope(db, profile_uuid, current_user, organization_id=ctx.org_id)

    # Update profile DB column for consistency
    profile.predicted_gender = gender  # type: ignore[assignment]

    # Bulk-update the caller's linked speakers in this tenant
    updated_count = (
        db.query(Speaker)
        .filter(
            Speaker.profile_id == profile.id,
            Speaker.user_id == current_user.id,
            Speaker.media_file_id.in_(file_ids_in_scope(ctx.org_id)),
        )
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
