"""A speaker's identity history: who named, linked or suggested it, when, and with which model.

Issue #1336. Read-only; the rows are written with each change (see
``app/services/speaker_identity_history.py``).
"""

from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.user import User
from app.schemas.speaker import SpeakerIdentityEvent
from app.services.request_context import RequestContext
from app.services.speaker_identity_history import list_identity_history
from app.services.speaker_provenance_view import ProvenanceViewer
from app.services.uuid_helpers import require_speaker_access

router = APIRouter()


@router.get("/{speaker_uuid}/identity-history", response_model=list[SpeakerIdentityEvent])
def get_speaker_identity_history(
    speaker_uuid: str,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> list[dict[str, Any]]:
    """The speaker's recorded name, link and suggestion changes, newest first.

    Anyone who may read the speaker may read its history (tenant, quarantine, share and platform
    grant are checked by ``require_speaker_access``). While the file's voiceprints are off, what
    a voiceprint produced is withheld; the person behind a change is named only to the viewers
    who see ``identity.by`` on the speaker itself.
    """
    speaker = require_speaker_access(
        db, speaker_uuid, current_user, organization_id=ctx.org_id, bypass=ctx.bypass
    )
    return list_identity_history(db, speaker, ProvenanceViewer.from_context(ctx), limit=limit)
