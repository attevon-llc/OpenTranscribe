"""Which per-user media-source rows a user may use (own, plus shared within a tenant).

The query the MediaCMS protected-media plugin reads its per-user sources through.
"""

from typing import Any


def query_user_media_sources(db: Any, user_id: int | None) -> list[Any]:
    """Per-user MediaCMS source rows ``user_id`` may use: own + shared in its tenant.

    Own sources sort first so the user's own credentials take priority. A source
    shared by another user is usable only when owner and user share a tenant — the
    stored credentials are the owner's. With no user there is no tenant to share
    within, so no per-user source is returned.
    """
    from sqlalchemy import and_
    from sqlalchemy import or_

    from app.models.user_media_source import UserMediaSource
    from app.utils.tenant_sharing import owner_shares_tenant_with

    if user_id is None:
        return []
    from app.core.capabilities import capability_enabled

    if not capability_enabled("media_sources"):
        # Per-user sources are a surface this deployment has turned off (issue #1109);
        # operator-configured sources still apply.
        return []
    rows: list[Any] = (
        db.query(UserMediaSource)
        .filter(
            UserMediaSource.provider_type == "mediacms",
            UserMediaSource.is_active.is_(True),
            or_(
                UserMediaSource.user_id == user_id,
                and_(
                    UserMediaSource.is_shared.is_(True),
                    owner_shares_tenant_with(UserMediaSource.user_id, user_id),
                ),
            ),
        )
        .order_by((UserMediaSource.user_id == user_id).desc())
        .all()
    )
    return rows
