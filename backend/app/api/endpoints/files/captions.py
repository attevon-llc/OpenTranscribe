"""The file-detail player's caption track: ``GET /files/{uuid}/captions`` (WebVTT).

The player used to build its WebVTT in the browser, one cue per diarized segment — a
minute-long turn covered the video with a paragraph, and the track was a client-side
serialization of transcript text, the shape issue #673 removed from every export.
The track is now cut here, by ``app.services.captions`` (the same segmenter as the
SRT/WebVTT downloads), and served inline for a ``<track>`` element.

**This is a transcript READ, not an export**, so it is gated exactly like
``GET /files/{uuid}/segments``, the endpoint whose text the page shows beside the
player: the reader's policy is resolved by ``_resolve_redaction_for_request`` (tenant
floor threaded, support grants see the owner's policy, ``?redact=false`` reveals only
what the caller may reveal and is audited), and while the file's redaction scan is
unfinished the track is withheld with **409**, as the transcript is. It does not take the
``export_locked`` floor: that floor governs files leaving the application, and applying it
here would mask the captions while the transcript beside them is shown in the clear.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.user import User
from app.services.captions import build_caption_track
from app.services.captions import to_webvtt
from app.services.captions.source import load_caption_segments

from .crud import _redaction_pending
from .crud import _resolve_redaction_for_request
from .crud import get_media_file_by_uuid

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/{file_uuid}/captions", response_class=Response)
def get_caption_track(
    file_uuid: UUID,
    redact: bool = Query(True, description="Apply content redaction (owner/admin may disable)"),
    speaker_placeholder: str | None = Query(
        None,
        max_length=64,
        description=(
            "How to show an unnamed diarization speaker, with a {number} token, e.g. "
            "'Speaker {number}'. Omitted: the raw SPEAKER_NN label."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> Response:
    """WebVTT captions for the player, cut into broadcast-style cues.

    Raises:
        HTTPException: 404 when the file has no completed transcript, 409 while its
            redaction scan has not finished, 503 when the redaction policy cannot be
            resolved.
    """
    db_file = get_media_file_by_uuid(
        db, str(file_uuid), current_user.id, bypass=ctx.bypass, organization_id=ctx.org_id
    )
    if db_file.status != "completed":
        raise HTTPException(status_code=404, detail="No transcript available for this file")

    cfg, reveal = _resolve_redaction_for_request(
        db, db_file, current_user, bypass=ctx.bypass, redact=redact, organization_id=ctx.org_id
    )
    if _redaction_pending(db, cfg, db_file):
        raise HTTPException(
            status_code=409,
            detail="Content redaction has not finished for this file; captions are withheld.",
        )

    try:
        segments = load_caption_segments(
            db,
            int(db_file.id),
            redaction_cfg=cfg,
            reveal_categories=reveal,
            placeholder_template=speaker_placeholder,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail="No transcript available for this file") from e

    return Response(
        content=to_webvtt(build_caption_track(segments)),
        media_type="text/vtt; charset=utf-8",
        # The cues depend on the reader's redaction policy and on speaker names that can
        # change at any moment; a cached copy would outlive both.
        headers={"Cache-Control": "no-store"},
    )
