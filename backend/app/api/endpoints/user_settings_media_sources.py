"""Per-user media source endpoints (with sharing)."""

from datetime import UTC

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.core.capabilities import require_capability
from app.db.base import get_db
from app.schemas.media_source import UserMediaSourceCreate
from app.schemas.media_source import UserMediaSourceResponse
from app.schemas.media_source import UserMediaSourcesList
from app.schemas.media_source import UserMediaSourceUpdate
from app.services.request_context import RequestContext
from app.utils.tenant_sharing import owner_in_tenant

router = APIRouter()


def _media_source_to_response(
    source: models.UserMediaSource,
    owner: models.User | None = None,
    is_own: bool = True,
) -> dict:
    """Build a UserMediaSourceResponse-compatible dict."""
    return {
        "uuid": str(source.uuid),
        "hostname": source.hostname,
        "provider_type": source.provider_type,
        "username": (source.username or "") if is_own else "",
        "has_credentials": bool(source.username and source.password),
        "verify_ssl": source.verify_ssl,
        "label": source.label or "",
        "is_active": source.is_active,
        "is_shared": source.is_shared,
        "shared_at": source.shared_at,
        "owner_name": (owner.full_name or owner.email) if owner else None,
        "owner_role": owner.role if owner else None,
        "is_own": is_own,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


@router.get(
    "/media-sources",
    response_model=UserMediaSourcesList,
    dependencies=[Depends(require_capability("media_sources", platform_admin_bypass=False))],
)
def get_media_sources(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> dict:
    """Get user's own media sources and sources shared within the caller's active tenant."""
    current_user = ctx.user
    # Own sources
    own_sources = (
        db.query(models.UserMediaSource)
        .filter(models.UserMediaSource.user_id == current_user.id)
        .order_by(models.UserMediaSource.created_at.desc())
        .all()
    )

    # Shared sources from other active users in the caller's tenant
    shared_sources = (
        db.query(models.UserMediaSource)
        .join(models.User, models.User.id == models.UserMediaSource.user_id)
        .filter(
            models.UserMediaSource.is_shared == True,  # noqa: E712
            models.UserMediaSource.is_active == True,  # noqa: E712
            models.UserMediaSource.user_id != current_user.id,
            models.User.is_active == True,  # noqa: E712
            owner_in_tenant(models.UserMediaSource.user_id, ctx.org_id),
        )
        .order_by(models.UserMediaSource.shared_at.desc().nullslast())
        .all()
    )

    # Batch-fetch owners for shared source attribution
    owner_ids = {s.user_id for s in shared_sources}
    owners = {}
    if owner_ids:
        owners = {
            u.id: u for u in db.query(models.User).filter(models.User.id.in_(owner_ids)).all()
        }

    return {
        "sources": [_media_source_to_response(s, current_user, is_own=True) for s in own_sources],
        "shared_sources": [
            _media_source_to_response(s, owners.get(s.user_id), is_own=False)
            for s in shared_sources
        ],
    }


@router.post(
    "/media-sources",
    response_model=UserMediaSourceResponse,
    dependencies=[Depends(require_capability("media_sources", platform_admin_bypass=False))],
)
def create_media_source(
    data: UserMediaSourceCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Create a new user media source."""
    import uuid as uuid_pkg

    from sqlalchemy.exc import IntegrityError

    from app.utils.encryption import encrypt_api_key
    from app.utils.encryption import test_encryption

    # Per-user rows (unlike admin's system-wide JSON list); enforce the per-user limit
    existing_count = (
        db.query(models.UserMediaSource)
        .filter(models.UserMediaSource.user_id == current_user.id)
        .count()
    )
    if existing_count >= 50:
        raise HTTPException(status_code=400, detail="Maximum number of media sources reached (50)")

    # Encrypt password if provided
    encrypted_password = None
    if data.password:
        if not test_encryption():
            raise HTTPException(status_code=500, detail="Encryption not available")
        encrypted_password = encrypt_api_key(data.password)
        if not encrypted_password:
            raise HTTPException(status_code=500, detail="Failed to encrypt password")

    new_source = models.UserMediaSource(
        uuid=uuid_pkg.uuid4(),
        user_id=current_user.id,
        hostname=data.hostname,
        provider_type=data.provider_type,
        username=data.username or None,
        password=encrypted_password,
        verify_ssl=data.verify_ssl,
        label=data.label or None,
    )
    db.add(new_source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"A source with hostname '{data.hostname}' already exists",
        ) from None
    db.refresh(new_source)

    return _media_source_to_response(new_source, current_user, is_own=True)


@router.put(
    "/media-sources/{source_uuid}",
    response_model=UserMediaSourceResponse,
    dependencies=[Depends(require_capability("media_sources", platform_admin_bypass=False))],
)
def update_media_source(
    source_uuid: str,
    data: UserMediaSourceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Update an existing user media source. Owner only."""
    from datetime import datetime

    from sqlalchemy.exc import IntegrityError

    from app.utils.encryption import encrypt_api_key
    from app.utils.encryption import test_encryption

    source = (
        db.query(models.UserMediaSource)
        .filter(
            models.UserMediaSource.uuid == source_uuid,
            models.UserMediaSource.user_id == current_user.id,
        )
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Media source not found")

    if data.hostname and data.hostname != source.hostname:
        source.hostname = data.hostname
    if data.provider_type is not None:
        source.provider_type = data.provider_type
    if data.username is not None:
        source.username = data.username or None
    if data.password is not None:
        if data.password:
            if not test_encryption():
                raise HTTPException(status_code=500, detail="Encryption not available")
            encrypted = encrypt_api_key(data.password)
            if not encrypted:
                raise HTTPException(status_code=500, detail="Failed to encrypt password")
            source.password = encrypted
        else:
            source.password = None
    if data.verify_ssl is not None:
        source.verify_ssl = data.verify_ssl
    if data.label is not None:
        source.label = data.label or None

    # Handle sharing toggle
    if data.is_shared is not None:
        if data.is_shared and not source.is_shared:
            source.is_shared = True
            source.shared_at = datetime.now(UTC)
        elif not data.is_shared and source.is_shared:
            source.is_shared = False
            source.shared_at = None

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"A source with hostname '{data.hostname or source.hostname}' already exists",
        ) from None
    db.refresh(source)
    return _media_source_to_response(source, current_user, is_own=True)


@router.delete(
    "/media-sources/{source_uuid}",
    dependencies=[Depends(require_capability("media_sources", platform_admin_bypass=False))],
)
def delete_media_source(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Delete a user media source. Owner only."""
    source = (
        db.query(models.UserMediaSource)
        .filter(
            models.UserMediaSource.uuid == source_uuid,
            models.UserMediaSource.user_id == current_user.id,
        )
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Media source not found")

    db.delete(source)
    db.commit()
    return {"success": True}
