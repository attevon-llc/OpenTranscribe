"""Watch-source routes that are not about one source: feature flags, the local folder picker,
the multipart-regex tester, and the DB-backed global settings (super_admin).

Static paths, registered on the parent router before ``/{source_uuid}``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth import get_current_active_user
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.schemas.watch_source import CapabilitiesResponse
from app.schemas.watch_source import DirectoryListResponse
from app.schemas.watch_source import MultipartRegexTestRequest
from app.schemas.watch_source import MultipartRegexTestResponse

logger = logging.getLogger(__name__)
router = APIRouter()


# --------------------------------------------------------------------------- #
# Static routes (declared before /{uuid})
# --------------------------------------------------------------------------- #
@router.get("/capabilities", response_model=CapabilitiesResponse)
def get_capabilities(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CapabilitiesResponse:
    """Feature-gating flags for the UI."""
    from app.services import watch_settings_service

    return CapabilitiesResponse(
        watch_source_enabled=watch_settings_service.is_enabled(db),
        local_enabled=bool(settings.WATCH_FOLDER_PATH),
        fs_events_enabled=watch_settings_service.fs_events_enabled(db),
        fs_events_mode=watch_settings_service.fs_events_mode(db),
    )


@router.get("/browse", response_model=DirectoryListResponse)
def browse_directories(
    path: str = Query("", description="Relative path under the watch root"),
    current_user: User = Depends(get_current_active_user),
) -> DirectoryListResponse:
    """List subdirectories under WATCH_FOLDER_PATH for the local folder picker."""
    from app.services.watch_sources import folder_browser

    if not settings.WATCH_FOLDER_PATH:
        raise HTTPException(status_code=404, detail="Local watch folder is not configured")
    try:
        return folder_browser.list_directories(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/test-multipart-regex", response_model=MultipartRegexTestResponse)
def test_multipart_regex(
    payload: MultipartRegexTestRequest,
    current_user: User = Depends(get_current_active_user),
) -> MultipartRegexTestResponse:
    """Parse a filename with a multipart regex (backend owns the regex)."""
    from app.services.watch_sources import multipart

    try:
        parsed = multipart.parse_part(payload.regex, payload.filename)
    except ValueError as e:
        return MultipartRegexTestResponse(matched=False, error=str(e))
    if not parsed:
        return MultipartRegexTestResponse(matched=False)
    base, part_num, ext = parsed
    return MultipartRegexTestResponse(
        matched=True, base_name=base, part_number=part_num, extension=ext
    )


# --------------------------------------------------------------------------- #
# Global settings (admin)
# --------------------------------------------------------------------------- #
@router.get("/settings")
def get_global_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    """Read the DB-backed global watch-source settings (super_admin only).

    Consumed by the admin Settings UI and by ops scripts checking a deployment's
    import tuning. These are ``SystemSettings`` rows, not ``.env`` vars — there is
    no environment fallback to read instead, and no restart applies them.
    """
    from app.services import watch_settings_service

    return watch_settings_service.get_global_settings(db)


@router.put("/settings")
def update_global_settings(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    """Update the global watch-source settings, taking effect without a restart.

    Consumed by the admin Settings UI and by ops automation. super_admin only —
    these knobs govern every user's imports, not just the caller's.

    The body is a loose ``dict`` rather than a schema so an older client keeps
    working: ``max_concurrent_imports`` is accepted as an alias for the post-#295
    ``max_imports_per_scan``. Only the new name is ever returned. Any key omitted
    is left at its current value by the service.
    """
    from app.services import watch_settings_service

    result = watch_settings_service.update_global_settings(
        db,
        enabled=payload.get("enabled"),
        file_stability_seconds=payload.get("file_stability_seconds"),
        # Accept the pre-#295 field name so an older client keeps working; the
        # response only ever carries the new name.
        max_imports_per_scan=payload.get(
            "max_imports_per_scan", payload.get("max_concurrent_imports")
        ),
        fs_events_enabled=payload.get("fs_events_enabled"),
        fs_events_mode=payload.get("fs_events_mode"),
        fs_events_poll_seconds=payload.get("fs_events_poll_seconds"),
    )
    logger.info("Admin %s updated global watch settings", current_user.email)
    return result
