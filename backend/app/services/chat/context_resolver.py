"""Resolve a chat scope (files / collections / tags) to concrete file UUIDs.

Scope resolution happens in **Postgres, not OpenSearch**. The index carries
denormalized ``collection_ids`` / ``tags`` / ``accessible_user_ids`` fields, but
those can lag a share change or a quarantine flag by a reindex; sharing and
takedown semantics are authoritative in the relational tables. Resolving here and
passing an explicit uuid list to the retriever means an unshared or quarantined
file cannot leak into a prompt through a stale document — **for a turn that
resolves to an explicit list.**

⚠️ That guarantee does NOT cover the unscoped case. ``resolve_scope_file_uuids``
returns ``None`` when ``scope.is_empty`` (see its docstring), which skips every
function in this module entirely — an empty scope was never a wide "no
predicate" pass through here, it is a request that never reaches it. Quarantine
is not an OpenSearch filter field, so an unscoped turn used to retrieve a
quarantined file's chunks and digest sections unfiltered; that gap is closed in
``service.py``'s ``_drop_quarantined_hits`` (phase 3.5 of ``_prepare_context``),
not in this module. Sharing is not similarly exposed on the unscoped path — the
index's ``accessible_user_ids`` term (``hybrid_search_service._build_filters``)
enforces it there — only quarantine, which the index carries no field for at
all, needed a second gate.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
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


def _visible_files_query(db: Session, ctx: RequestContext):
    """Base query over files the caller may read, in their tenant scope.

    Every caller still joins through, or filters by, an already-authorized
    relation — an accessible collection, an accessible-files subquery, or an
    explicit permission check — so this alone is never the whole access rule.
    It used to take an ``owned_only`` flag restricting to the caller's own
    files for the context-size estimator's "all transcripts" branch; that
    branch now counts through the same accessible-files subquery every other
    axis in this module uses (see ``count_scope_files``), so nothing calls it
    with ``owned_only`` any more and the flag was deleted rather than left as
    a second, unused way to say the same thing.
    """
    query = db.query(MediaFile.uuid).filter(MediaFile.status == "completed")

    if ctx.org_id is not None:
        query = query.filter(MediaFile.organization_id == ctx.org_id)
    else:
        query = query.filter(MediaFile.organization_id.is_(None))

    # Quarantined files are invisible here to EVERYONE, admins included — no
    # exception, unlike the review surfaces (issue #262g originally carved one
    # out; W2.0g's adversarial review removed it). Chat is a retrieval surface,
    # not a review one (see this module's docstring and `_resolve_explicit_files`
    # below), and `service.py`'s phase-3.5 `_drop_quarantined_hits` already drops
    # a quarantined file's hits for an admin's unscoped turn unconditionally. An
    # admin-only bypass HERE — reachable only through the collection/tag axes and
    # `count_scope_files`'s "All transcripts" estimate — would disagree with that
    # unconditional drop and with the explicit-file axis, which has never had an
    # admin bypass (see `_resolve_explicit_files`): three axes admitting a
    # quarantined file into scope while the fourth (phase 3.5) always removes it
    # is the exact "two access rules disagreeing" shape this module's admin
    # bypasses were already removed for. Admins keep the ordinary admin review
    # UI (`GET /admin/files/quarantined`) for quarantined content.
    query = query.filter(MediaFile.is_quarantined.is_(False))

    return query


def _resolve_explicit_document(db: Session, ctx: RequestContext, document_uuid: str) -> str | None:
    """Resolve one uuid as a Document chat may retrieve from, else ``None``.

    v400 (#362 lane C3-remainder). The picker's ``PickerDocumentsTab.svelte`` writes
    selected document uuids into the SAME ``ChatScope.file_uuids`` array
    ``PickerFilesTab.svelte`` uses — deliberately, because ``index_document_chunks``
    stamps a document's chunks with the ``file_uuid`` field exactly like a media
    file's, so one resolved uuid list already means "search these file_uuids" to
    every downstream retrieval call. This is the "try Document when MediaFile lookup
    misses" extension that component's own docstring names as the missing half.

    Same rule :func:`_resolve_explicit_files` applies to a media file: no admin
    bypass (``PermissionService.get_document_permission`` has none — an admin-only
    bypass here would resolve a document into scope that retrieval's
    ``accessible_user_ids`` OpenSearch filter then serves nothing for, the exact
    "two access rules disagree" shape that function's docstring argues against),
    completed only, and a quarantined document is excluded for every caller
    (``_visible_files_query``'s media-file quarantine exclusion has no exception
    either — chat is a retrieval surface, not the admin review one).
    """
    from app.models.document import Document
    from app.services.permission_service import PermissionService

    doc = db.query(Document).filter(Document.uuid == document_uuid).first()
    if doc is None:
        return None
    if bool(doc.is_quarantined):
        return None
    status_value = doc.status.value if hasattr(doc.status, "value") else str(doc.status)
    if status_value != "completed":
        return None
    permission = PermissionService.get_document_permission(
        db, doc.id, ctx.user.id, organization_id=ctx.org_id
    )
    if permission is None:
        return None
    return str(doc.uuid)


def _resolve_explicit_files(db: Session, ctx: RequestContext, file_uuids: list[str]) -> set[str]:
    """Permission-check each explicitly selected file, skipping inaccessible ones.

    ``is_admin`` is deliberately **not** threaded through to the permission
    check, even for a caller who genuinely is an admin. The admin bypass in
    ``get_file_by_uuid_with_permission`` exists for the *review* surfaces
    (file detail/stream/download) and has no counterpart in the chat
    retrieval plane: ``hybrid_search_service._build_filters`` gates every
    OpenSearch read on ``accessible_user_ids`` with no admin arm at all. Scope
    resolution used to pass the caller's real admin flag through, so a file an
    admin has neither ownership nor a share of would resolve HERE and then
    retrieve nothing — a silent, unexplained empty answer, not a leak, but the
    exact shape of one: two access rules for the same file that disagree.
    Resolving as an ordinary user makes this axis agree with what retrieval
    can actually serve. Admins keep the ordinary review path (the admin UI)
    for quarantined content; chat is a retrieval surface, not a review one.

    A uuid that misses against ``MediaFile`` is tried against ``Document``
    (:func:`_resolve_explicit_document`) before being counted as inaccessible —
    see that function's docstring for why one array can hold both kinds.
    """
    resolved: set[str] = set()
    for file_uuid in file_uuids:
        try:
            media_file = get_file_by_uuid_with_permission(
                db,
                file_uuid,
                ctx.user.id,
                is_admin=False,
                organization_id=ctx.org_id,
            )
        except HTTPException:
            document_uuid = _resolve_explicit_document(db, ctx, file_uuid)
            if document_uuid is not None:
                resolved.add(document_uuid)
            else:
                logger.info("Chat scope: skipping inaccessible file %s", file_uuid)
            continue
        if media_file.status != "completed":
            continue
        resolved.add(str(media_file.uuid))
    return resolved


def _resolve_collections(db: Session, ctx: RequestContext, collection_uuids: list[str]) -> set[str]:
    """Expand collections the caller owns or has shared access to.

    No admin bypass, on purpose (same reasoning as ``_resolve_explicit_files``):
    an admin-only bypass here used to let a requested collection resolve
    without the caller actually having a share of it, but retrieval's
    ``accessible_user_ids`` OpenSearch filter has no admin arm to match — the
    files would resolve into scope and then retrieve zero excerpts, an
    unexplained empty answer rather than the intended "admin can see
    everything". Resolving as an ordinary user keeps this axis honest about
    what the turn can actually retrieve.
    """
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
    allowed_ids = list(requested_ids & accessible)
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
    """Expand tags to every file the caller can READ that carries them.

    Routed through ``get_accessible_file_ids_subquery`` — the same sharing rule
    ``endpoints/tags.py:_visible_to`` and every owner-scoped listing use, which
    already covers owned files plus collections shared directly and via groups,
    and applies the tenant gate. This axis used to filter on
    ``MediaFile.user_id == ctx.user.id`` while ``_resolve_collections`` honoured
    sharing, so scoping a chat by a tag spanning shared recordings silently
    dropped them and the model answered from the remainder with no signal that
    anything had been excluded (issue #385).

    Tag names are unique **per owner**, so matching by name deliberately spans
    every user's tag row: the sharee's "atlas" scope must reach the owner's
    atlas-tagged recording they were given access to.

    No admin bypass — same as every other axis in this module (``_resolve_
    collections`` included; it lost its own admin bypass in the same pass this
    one did, so the "unlike collections" phrasing this docstring used to carry
    was already stale). A collection or an explicit file is named by uuid; a
    tag name is a wide net, and giving admins a tenant-wide one here would
    resolve tags their own tag picker (``_visible_to``) never shows them.
    Admins do NOT keep quarantine visibility here either: ``_visible_files_
    query`` excludes a quarantined file for every caller, admin included.
    """
    if not tag_names:
        return set()

    from app.services.permission_service import PermissionService

    accessible_files = PermissionService.get_accessible_file_ids_subquery(
        db, ctx.user.id, organization_id=ctx.org_id
    )
    rows = (
        _visible_files_query(db, ctx)
        .join(FileTag, FileTag.media_file_id == MediaFile.id)
        .join(Tag, Tag.id == FileTag.tag_id)
        .filter(Tag.name.in_(tag_names))
        .filter(MediaFile.id.in_(select(accessible_files)))
        .all()
    )
    return {str(row[0]) for row in rows}


def resolve_scope_file_uuids(
    db: Session,
    ctx: RequestContext,
    scope: ChatScope,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> list[str] | None:
    """Resolve a chat scope to the file UUIDs retrieval may search.

    Args:
        db: Database session.
        ctx: Request context (user + tenant scope).
        scope: The conversation's pinned or per-request scope.
        diagnostics: Optional out-param (same shape as
            ``search.chunk_retrieval.retrieve_chunks``'s ``diagnostics``
            param). When given, ``diagnostics["files_dropped"]`` is set to how
            many of ``scope.file_uuids`` — the EXPLICIT picks, the axis a file
            picker UI offers — could not be resolved (inaccessible, deleted,
            or quarantined), never set at all when none were. Without this an
            admin who picks 40 recordings the scope then resolves to 3 gets an
            answer that covers 3 while `files_searched` reports nothing was
            excluded — `_resolve_explicit_files` only logs the skip
            (``logger.info``), which is invisible outside a log tail.

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

    explicit_resolved = _resolve_explicit_files(db, ctx, scope.file_uuids)
    if diagnostics is not None and scope.file_uuids:
        dropped = len(set(scope.file_uuids)) - len(explicit_resolved)
        if dropped > 0:
            diagnostics["files_dropped"] = dropped

    resolved = set(explicit_resolved)
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
        # "All transcripts" — count every file retrieval would actually search:
        # owned AND shared, through the same accessible-files subquery
        # `_resolve_tags` uses. This used to count owned files only, so a
        # caller whose library is entirely shared-with-them saw the estimator
        # report "0 recordings, ~0% of context" for a scope that in fact
        # searches everything they can read (the OVER-restriction half of this
        # fix; the LEAK half would be reporting files the caller cannot read,
        # which this cannot do — it is the identical rule retrieval's
        # `accessible_user_ids` term enforces, not a wider one).
        from app.services.permission_service import PermissionService

        accessible = PermissionService.get_accessible_file_ids_subquery(
            db, ctx.user.id, organization_id=ctx.org_id
        )
        return int(
            _visible_files_query(db, ctx).filter(MediaFile.id.in_(select(accessible))).count()
        )

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
