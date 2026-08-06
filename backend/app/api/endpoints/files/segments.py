"""Lightweight transcript segments endpoint for pagination.

Returns only transcript segments with pagination metadata,
avoiding the 5+ extra queries that the full file detail endpoint runs.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.user import User
from app.schemas.media import TranscriptSegmentsPage
from app.services.speaker_status_service import SpeakerStatusService

from .crud import _build_grouped_segments
from .crud import _format_transcript_segments
from .crud import _get_transcript_segments
from .crud import _redaction_pending
from .crud import _resolve_redaction_for_request
from .crud import get_media_file_by_uuid

router = APIRouter()
logger = logging.getLogger(__name__)

# Upper bound on one page. `handleLoadUpTo` in the SPA asks for however many segments
# stand between the loaded tail and a jump target, so an unbounded limit lets a
# jump-to-end on a 50k-segment file request the whole transcript in one call. The SPA
# pages in a loop instead.
MAX_SEGMENT_PAGE_SIZE = 2000


@router.get("/{file_uuid}/segments", response_model=TranscriptSegmentsPage)
def get_file_segments(
    file_uuid: UUID,
    segment_limit: int = Query(
        500, ge=1, le=MAX_SEGMENT_PAGE_SIZE, description="Number of segments to return"
    ),
    segment_offset: int = Query(0, ge=0, description="Offset for pagination"),
    redact: bool = Query(True, description="Apply content redaction (owner/admin may disable)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """Get paginated transcript segments for a file.

    Lightweight endpoint for "load more" transcript pagination. Returns segments, the
    display grouping that references them, and the total count — no tags, collections,
    speakers list, analytics, or other file metadata.

    ``grouped_segments`` is what the SPA renders, so omitting it here meant paginated
    segments were fetched but never displayed (issue #352). Grouping is O(n) over
    already-materialized objects and adds no query, so the endpoint keeps its
    two-query profile.
    """
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(
        db, str(file_uuid), current_user.id, is_admin=is_admin, organization_id=ctx.org_id
    )
    file_id = db_file.id
    redaction_cfg, reveal_categories = _resolve_redaction_for_request(
        db, db_file, current_user, is_admin=is_admin, redact=redact
    )

    # Withhold the transcript until redaction finishes (when enabled).
    if _redaction_pending(db, redaction_cfg, db_file):
        return {
            "transcript_segments": [],
            "grouped_segments": [],
            "total_segments": 0,
            "redaction_pending": True,
            "redaction_status": str(db_file.redaction_status) if db_file.redaction_status else None,
        }

    # 2 queries: count + paginated select with joinedload (vs 8+ in full detail)
    transcript_segments, total_segments = _get_transcript_segments(
        db, file_id, segment_limit, segment_offset
    )

    # Add computed status to segment speakers for resolved_display_name
    processed_ids: set[int] = set()
    unique_speakers = []
    for segment in transcript_segments:
        if segment.speaker and int(segment.speaker.id) not in processed_ids:
            SpeakerStatusService.add_computed_status(segment.speaker)
            processed_ids.add(int(segment.speaker.id))
            unique_speakers.append(segment.speaker)

    formatted_segments = _format_transcript_segments(
        transcript_segments, unique_speakers, redaction_cfg, reveal_categories
    )

    return {
        "transcript_segments": formatted_segments,
        "grouped_segments": _build_grouped_segments(
            formatted_segments, index_offset=segment_offset
        ),
        "total_segments": total_segments,
    }
