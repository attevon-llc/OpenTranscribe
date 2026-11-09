"""The default-deny tenancy criterion for a request, as plain SQL."""

from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy import and_

from app.services.request_context import RequestContext


def context_scope_criterion(model: Any, ctx: RequestContext) -> ColumnElement[bool]:
    """Org scope when active, else the caller's personal rows.

    Personal scope = the user's PERSONAL rows (``organization_id IS NULL``): a file uploaded
    into an org belongs to the org space, not the uploader's personal space. In the community
    edition ``organization_id`` is always NULL, so this is behavior-identical to plain user_id
    filtering there.

    ``model`` must expose ``organization_id`` and ``user_id`` columns.
    """
    in_org: ColumnElement[bool] = model.organization_id == ctx.org_id
    if ctx.is_org_context:
        return in_org
    return and_(model.user_id == ctx.user.id, model.organization_id.is_(None))
