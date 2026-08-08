"""Resolve a chat scope (files / collections / tags) to concrete file UUIDs.

Scope resolution happens in **Postgres, not OpenSearch**. The index carries
denormalized ``collection_ids`` / ``tags`` / ``accessible_user_ids`` fields, but
those can lag a share change or a quarantine flag by a reindex; sharing and
takedown semantics are authoritative in the relational tables. Resolving here and
passing an explicit uuid list to the retriever means an unshared or quarantined
file cannot leak into a prompt through a stale document.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.core import constants as C  # noqa: N812
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.schemas.chat import ChatScope
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

logger = logging.getLogger(__name__)


def _visible_files_query(db: Session, ctx: RequestContext, *, owned_only: bool = False):
    """Base query over files the caller may read, in their tenant scope.

    ``owned_only`` restricts to the caller's OWN files. Callers that join through
    an already-authorized relation (a collection they can access) leave it off;
    callers that would otherwise enumerate the whole tenant — notably the
    context-size estimator's "all transcripts" branch — must set it, or the
    returned count discloses how many recordings other users have.
    """
    query = db.query(MediaFile.uuid).filter(MediaFile.status == "completed")

    if ctx.org_id is not None:
        query = query.filter(MediaFile.organization_id == ctx.org_id)
    else:
        query = query.filter(MediaFile.organization_id.is_(None))

    # Quarantined files are invisible to everyone but admins (issue #262g).
    if not ctx.user.is_admin:
        query = query.filter(MediaFile.is_quarantined.is_(False))

    if owned_only:
        query = query.filter(MediaFile.user_id == ctx.user.id)

    return query


def _resolve_explicit_files(db: Session, ctx: RequestContext, file_uuids: list[str]) -> set[str]:
    """Permission-check each explicitly selected file, skipping inaccessible ones."""
    resolved: set[str] = set()
    for file_uuid in file_uuids:
        try:
            media_file = get_file_by_uuid_with_permission(
                db,
                file_uuid,
                ctx.user.id,
                is_admin=bool(ctx.user.is_admin),
                organization_id=ctx.org_id,
            )
        except HTTPException:
            logger.info("Chat scope: skipping inaccessible file %s", file_uuid)
            continue
        if media_file.status != "completed":
            continue
        if bool(media_file.is_quarantined) and not ctx.user.is_admin:
            continue
        resolved.add(str(media_file.uuid))
    return resolved


def _resolve_collections(db: Session, ctx: RequestContext, collection_uuids: list[str]) -> set[str]:
    """Expand collections the caller owns or has shared access to."""
    if not collection_uuids:
        return set()

    from app.services.permission_service import PermissionService

    requested = db.query(Collection.id).filter(Collection.uuid.in_(collection_uuids))
    if ctx.org_id is not None:
        requested = requested.filter(Collection.organization_id == ctx.org_id)
    else:
        requested = requested.filter(Collection.organization_id.is_(None))
    requested_ids = {row[0] for row in requested.all()}
    if not requested_ids:
        return set()

    # One query for everything the caller can reach (owned + direct + group shares).
    accessible = {
        cid for cid, _perm in PermissionService.get_accessible_collection_ids(db, ctx.user.id)
    }
    allowed_ids = list(requested_ids if ctx.user.is_admin else requested_ids & accessible)
    if not allowed_ids:
        return set()

    rows = (
        _visible_files_query(db, ctx)
        .join(CollectionMember, CollectionMember.media_file_id == MediaFile.id)
        .filter(CollectionMember.collection_id.in_(allowed_ids))
        .all()
    )
    return {str(row[0]) for row in rows}


def _resolve_tags(db: Session, ctx: RequestContext, tag_names: list[str]) -> set[str]:
    """Expand tags to the caller's own files carrying them."""
    if not tag_names:
        return set()

    rows = (
        _visible_files_query(db, ctx)
        .join(FileTag, FileTag.media_file_id == MediaFile.id)
        .join(Tag, Tag.id == FileTag.tag_id)
        .filter(Tag.name.in_(tag_names))
        .filter(MediaFile.user_id == ctx.user.id)
        .all()
    )
    return {str(row[0]) for row in rows}


def resolve_scope_file_uuids(
    db: Session, ctx: RequestContext, scope: ChatScope
) -> list[str] | None:
    """Resolve a chat scope to the file UUIDs retrieval may search.

    Args:
        db: Database session.
        ctx: Request context (user + tenant scope).
        scope: The conversation's pinned or per-request scope.

    Returns:
        ``None`` when the scope is empty — meaning "every transcript the caller
        can access", enforced downstream by the ``accessible_user_ids`` term
        rather than by an enumerated list. Otherwise the union of the resolved
        files (possibly empty, which correctly matches nothing).

    Raises:
        HTTPException: 400 when the scope resolves to more files than
            :data:`app.core.constants.CHAT_MAX_SCOPE_FILES`.
    """
    if scope.is_empty:
        return None

    resolved = _resolve_explicit_files(db, ctx, scope.file_uuids)
    resolved |= _resolve_collections(db, ctx, scope.collection_uuids)
    resolved |= _resolve_tags(db, ctx, scope.tag_names)

    if len(resolved) > C.CHAT_MAX_SCOPE_FILES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Selection resolves to {len(resolved)} files; "
                f"the maximum is {C.CHAT_MAX_SCOPE_FILES}. Narrow the selection."
            ),
        )

    logger.info(
        "Chat scope resolved: %d files (from %d files, %d collections, %d tags)",
        len(resolved),
        len(scope.file_uuids),
        len(scope.collection_uuids),
        len(scope.tag_names),
    )
    return sorted(resolved)


def count_scope_files(db: Session, ctx: RequestContext, scope: ChatScope) -> int:
    """File count for a scope, for the context-size estimator.

    An empty scope counts every accessible completed transcript, which is what
    "All transcripts" would actually search.
    """
    if scope.is_empty:
        # "All transcripts" — count only what this user owns. Retrieval is gated
        # by accessible_user_ids regardless; this is about not leaking a count.
        return int(_visible_files_query(db, ctx, owned_only=True).count())

    # Count without enforcing the 500-file ceiling: the estimator exists to WARN
    # about oversized selections, so raising there would silence it exactly when
    # it is most useful.
    try:
        resolved = resolve_scope_file_uuids(db, ctx, scope)
    except HTTPException as exc:
        if exc.status_code != 400:
            raise
        return C.CHAT_MAX_SCOPE_FILES + 1
    return len(resolved) if resolved is not None else 0
