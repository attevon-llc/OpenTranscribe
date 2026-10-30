"""Tenant gate for the per-source watch-source routes.

``_get_source_or_404`` is the one chokepoint every ``/{source_uuid}`` route resolves its row
through: another tenant's source is indistinguishable from a missing one (404), and another
user's source in the caller's own tenant is 403. The platform bypass is decided on the loaded
row. Shared by ``watch_sources.py``, ``watch_sources_files.py`` and
``watch_sources_email_links.py``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.watch_source import WatchSource
from app.services.request_context import RequestContext


def _source_in_tenant(source: WatchSource, ctx: RequestContext) -> bool:
    """A source belongs to the tenant it was created in (NULL = personal)."""
    return source.organization_id == ctx.org_id


def _get_source_or_404(
    db: Session,
    source_uuid: str,
    ctx: RequestContext,
    *,
    need: Literal["read", "write"] = "write",
) -> WatchSource:
    current_user = ctx.user
    source: WatchSource | None = (
        db.query(WatchSource).filter(WatchSource.uuid == source_uuid).first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Watch source not found")
    # The platform role operates every tenant's sources in single-tenant mode only; it
    # is decided on the loaded row, so multi-tenant mode refuses it like any outsider.
    if ctx.bypass.allows(
        org_id=source.organization_id,
        owner_id=source.user_id,
        need=need,
        resource_type="watch_source",
        resource_uuid=str(source.uuid),
    ):
        return source
    # Another tenant's source is indistinguishable from a missing one.
    if not _source_in_tenant(source, ctx):
        raise HTTPException(status_code=404, detail="Watch source not found")
    if source.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this watch source")
    return source
