"""Destructive tag operations: impact preview, rename, merge, delete, cleanup, promote.

Callers pass internal ids already authorized by the endpoint layer
(``tags._common._writable_tag_ids``); nothing here re-checks permissions.

Every mutation locks the tag rows it touches in ascending id order (so two
opposite merges cannot deadlock), commits once on success, and then calls
:func:`on_tags_changed` with the files whose tags changed: the reindex task
reads rows back from its own session, so it must run after the commit.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import delete
from sqlalchemy import distinct
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import not_
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import TAG_SOURCE_AI_ACCEPTED
from app.core.constants import TAG_SOURCE_AUTO_AI
from app.core.constants import TAG_SOURCE_MANUAL
from app.core.exceptions import OpenTranscribeError
from app.models.sharing import TagShare
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.hold_guard import assert_content_mutable
from app.services.tag_scope import accessible_file_ids_subquery
from app.services.tag_scope import is_system_tag
from app.services.tag_scope import system_tag
from app.services.tag_scope import tenant_key
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import clean_tag_name
from app.services.tag_service import lookup_existing_tag
from app.services.tag_service import normalize_tag_name
from app.services.tag_service import on_tags_changed
from app.services.tag_service import stored_normalized_name
from app.services.tag_stored_names import rewrite_stored_tag_names

logger = logging.getLogger(__name__)

__all__ = [
    "TAG_NOT_FOUND",
    "TagImpactEntry",
    "TagImpactReport",
    "TagMutationOutcome",
    "TagNotFoundError",
    "TagTenantMismatchError",
    "cleanup_unreferenced_tags",
    "delete_tags",
    "lock_tags",
    "merge_tags",
    "preview_tag_impact",
    "promote_tags_to_shared",
    "rename_tag",
    "rewrite_stored_tag_names",
    "touches_system_tag",
]

#: The only message a :class:`TagNotFoundError` carries; it is also the 404 detail.
TAG_NOT_FOUND = "Tag not found"


class TagTenantMismatchError(OpenTranscribeError):
    """A merge would fold a tag into a tag of another tenant."""


class TagNotFoundError(OpenTranscribeError):
    """A referenced tag does not exist (any more).

    Always raised with :data:`TAG_NOT_FOUND`, so a row that vanished mid-request
    reads exactly like an unknown uuid and no internal id reaches a response.
    """


@dataclass(frozen=True)
class TagImpactEntry:
    """File counts for one tag."""

    uuid: UUID
    name: str
    accessible_file_count: int
    total_file_count: int


@dataclass(frozen=True)
class TagImpactReport:
    """File counts for a set of tags; the totals count each file once."""

    tags: list[TagImpactEntry] = field(default_factory=list)
    accessible_file_count: int = 0
    total_file_count: int = 0


@dataclass
class TagMutationOutcome:
    """Result of a mutation, serialized as ``schemas.tag.TagMutationResult``."""

    impact: TagImpactReport
    tag: Tag | None = None
    merged: bool = False
    requires_confirmation: bool = False
    deleted_uuids: list[UUID] = field(default_factory=list)


# Least to most human. NULL (legacy) and unknown origins rank as manual.
_ORIGIN_RANK = {TAG_SOURCE_AUTO_AI: 0, TAG_SOURCE_AI_ACCEPTED: 1, TAG_SOURCE_MANUAL: 2}


def _origin_rank(origin: str | None) -> int:
    return _ORIGIN_RANK.get(origin or TAG_SOURCE_MANUAL, _ORIGIN_RANK[TAG_SOURCE_MANUAL])


def _promote_origin(keep: str | None, other: str | None) -> str | None:
    """The more human of two association origins, preferring ``keep`` on a tie.

    A NULL ``other`` that outranks ``keep`` is written as ``"manual"``, which
    repairs the legacy row; two NULLs stay NULL.
    """
    if _origin_rank(other) > _origin_rank(keep):
        return other if other is not None else TAG_SOURCE_MANUAL
    return keep


def _better_confidence(a: float | None, b: float | None) -> float | None:
    """The higher of two confidences; a missing value loses to any number."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def lock_tags(db: Session, tag_ids: Iterable[int]) -> dict[int, Tag]:
    """Lock the given tag rows with one ``SELECT ... ORDER BY tag.id ... FOR UPDATE``.

    Returns:
        The rows found, by id. A missing id is simply absent.
    """
    ids = sorted({int(i) for i in tag_ids})
    if not ids:
        return {}
    # Sessions do not autoflush: write pending changes first so the refresh
    # below re-reads them instead of discarding them.
    db.flush()
    rows = (
        db.query(Tag)
        .filter(Tag.id.in_(ids))
        .order_by(Tag.id)
        .with_for_update()
        .populate_existing()
        .all()
    )
    return {tag.id: tag for tag in rows}


def touches_system_tag(tags: Iterable[Any]) -> bool:
    """Whether any of ``tags`` is a system tag (its change reaches every account)."""
    return any(is_system_tag(tag) for tag in tags)


def _require_all(locked: dict[int, Tag], ids: Iterable[int]) -> list[Tag]:
    try:
        return [locked[i] for i in ids]
    except KeyError:
        raise TagNotFoundError(TAG_NOT_FOUND) from None


def _file_ids_of(db: Session, tag_ids: Sequence[int]) -> list[int]:
    rows = (
        db.query(FileTag.media_file_id)
        .filter(FileTag.tag_id.in_(tag_ids), FileTag.media_file_id.is_not(None))
        .order_by(FileTag.id)
        .all()
    )
    return list(dict.fromkeys(int(fid) for (fid,) in rows))


def preview_tag_impact(
    db: Session,
    tag_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: int | None = None,
    association_source: str | None = None,
) -> TagImpactReport:
    """Count the files a set of tags is on, before an operation changes them.

    Args:
        db: Session.
        tag_ids: Tags in the order their entries should be reported; unknown
            ids are skipped.
        user_id: The caller, for the accessible counts.
        organization_id: The request tenant; None is personal scope, not unscoped.
        association_source: Count only associations of this origin.

    Returns:
        Per-tag accessible and deployment-wide counts, plus union totals.
    """
    if not tag_ids:
        return TagImpactReport()
    unique_ids = sorted({int(i) for i in tag_ids})
    accessible = select(accessible_file_ids_subquery(db, user_id, organization_id))
    file_id = FileTag.media_file_id
    accessible_file = case((file_id.in_(accessible), file_id))
    conditions: list[Any] = [FileTag.tag_id.in_(unique_ids), file_id.is_not(None)]
    if association_source is not None:
        conditions.append(FileTag.source == association_source)

    per_tag = {
        int(tag_id): (int(open_count), int(total))
        for tag_id, open_count, total in db.query(
            FileTag.tag_id, func.count(distinct(accessible_file)), func.count(distinct(file_id))
        )
        .filter(*conditions)
        .group_by(FileTag.tag_id)
    }
    open_union, total_union = (
        db.query(func.count(distinct(accessible_file)), func.count(distinct(file_id)))
        .filter(*conditions)
        .one()
    )
    tags = {tag.id: tag for tag in db.query(Tag).filter(Tag.id.in_(unique_ids))}
    entries = []
    for tag_id in tag_ids:
        tag = tags.get(int(tag_id))
        if tag is None:
            continue
        open_count, total = per_tag.get(tag.id, (0, 0))
        entries.append(TagImpactEntry(tag.uuid, tag.name, open_count, total))
    return TagImpactReport(entries, int(open_union), int(total_union))


def _move_associations(db: Session, survivor: Tag, doomed_ids: list[int]) -> list[int]:
    """Repoint the doomed tags' associations to the survivor; return the files touched.

    A file that ends up carrying the survivor twice keeps one row, carrying the
    more human origin and the higher confidence. The duplicates are deleted and
    flushed before anything is repointed, so ``UNIQUE(media_file_id, tag_id)``
    never sees both rows of a pair.
    """
    links = db.query(FileTag).filter(FileTag.tag_id.in_(doomed_ids)).order_by(FileTag.id).all()
    affected = list(
        dict.fromkeys(int(link.media_file_id) for link in links if link.media_file_id is not None)
    )
    # A held file's tags are preserved as they were (legal-hold freeze): a fold that would
    # re-point one is refused before anything moves.
    assert_content_mutable(db, affected)
    keeper: dict[int, FileTag] = {
        int(link.media_file_id): link
        for link in db.query(FileTag).filter(
            FileTag.tag_id == survivor.id, FileTag.media_file_id.in_(affected)
        )
        if link.media_file_id is not None
    }
    collapsed: list[FileTag] = []
    moving: list[FileTag] = []
    for link in links:
        kept = keeper.get(link.media_file_id) if link.media_file_id is not None else None
        if kept is None:
            moving.append(link)
            if link.media_file_id is not None:
                keeper[int(link.media_file_id)] = link
            continue
        kept.source = _promote_origin(kept.source, link.source)
        kept.ai_confidence = _better_confidence(kept.ai_confidence, link.ai_confidence)
        collapsed.append(link)
    for link in collapsed:
        db.delete(link)
    db.flush()
    for link in moving:
        link.tag_id = survivor.id
    db.flush()
    return affected


def _move_share_grants(db: Session, survivor: Tag, doomed_ids: list[int]) -> None:
    """Hand the doomed tags' grants to the survivor, one per grantee.

    Nothing moves onto a system tag: every account already sees it. Grants left
    behind are removed with their tag by the foreign key cascade.
    """
    if is_system_tag(survivor):
        return
    held = {
        (grant.target_user_id, grant.target_group_id)
        for grant in db.query(TagShare).filter(TagShare.tag_id == survivor.id)
    }
    grants = db.query(TagShare).filter(TagShare.tag_id.in_(doomed_ids)).order_by(TagShare.id)
    for grant in grants:
        grantee = (grant.target_user_id, grant.target_group_id)
        if grantee in held:
            continue
        if grant.target_user_id is not None and grant.target_user_id == survivor.user_id:
            continue
        grant.tag_id = survivor.id
        held.add(grantee)
    db.flush()


def _fold(db: Session, survivor: Tag, doomed: list[Tag]) -> list[int]:
    """Fold ``doomed`` into ``survivor``: associations, grants, stored names, rows.

    Flushes but never commits (merge and promote own the transaction).

    Returns:
        The files whose tag set changed.
    """
    doomed_ids = [tag.id for tag in doomed]
    affected = _move_associations(db, survivor, doomed_ids)
    _move_share_grants(db, survivor, doomed_ids)
    if survivor.source == TAG_SOURCE_AUTO_AI:
        survivor.source = TAG_SOURCE_AI_ACCEPTED
    by_tenant: dict[tuple[Any, ...], set[str]] = {}
    for tag in doomed:
        by_tenant.setdefault(tenant_key(tag), set()).add(stored_normalized_name(tag))
    for tenant, names in by_tenant.items():
        rewrite_stored_tag_names(db, names, survivor.name, tenant=tenant)
    for tag in doomed:
        db.delete(tag)
    db.flush()
    return affected


def merge_tags(
    db: Session,
    target_id: int,
    source_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: int | None = None,
) -> TagMutationOutcome:
    """Fold the source tags into the target tag.

    Raises:
        TagNotFoundError: The target or a source no longer exists (so the second
            of two opposite merges fails instead of destroying the first's survivor).
        TagTenantMismatchError: A source belongs to another tenant than the
            target. Folding into a system tag is always allowed.
        HoldRefusedError: A source tag is on a file under a legal hold.
    """
    locked = lock_tags(db, [target_id, *source_ids])
    target = locked.get(target_id)
    if target is None:
        raise TagNotFoundError(TAG_NOT_FOUND)
    doomed_ids = sorted({int(i) for i in source_ids} - {target_id})
    doomed = _require_all(locked, doomed_ids)
    if not is_system_tag(target) and any(tenant_key(t) != tenant_key(target) for t in doomed):
        raise TagTenantMismatchError("Tags from different tenants cannot be merged")

    impact = preview_tag_impact(db, doomed_ids, user_id=user_id, organization_id=organization_id)
    if not doomed:
        return TagMutationOutcome(impact=impact, tag=target)

    system_scope = touches_system_tag([target, *doomed])
    deleted_uuids = [tag.uuid for tag in doomed]
    affected = _fold(db, target, doomed)
    db.commit()
    on_tags_changed(db, affected, user_id=user_id, system_scope=system_scope)
    return TagMutationOutcome(impact=impact, tag=target, merged=True, deleted_uuids=deleted_uuids)


def _rename_collision(
    db: Session, tag: Tag, normalized: str, cleaned: str, user_id: int
) -> Tag | None:
    """The row the new name already belongs to, looked up in the renamed tag's tenant."""
    if is_system_tag(tag):
        return (
            db.query(Tag)
            .filter(system_tag(), Tag.normalized_name == normalized, Tag.id != tag.id)
            .order_by(Tag.id)
            .first()
        )
    owner = tag.user_id if tag.user_id is not None else user_id
    return lookup_existing_tag(db, normalized, cleaned, owner, tag.organization_id)


def rename_tag(
    db: Session,
    tag_id: int,
    new_name: str,
    *,
    confirm_merge: bool = False,
    user_id: int,
    organization_id: int | None = None,
) -> TagMutationOutcome:
    """Rename a tag; onto another tag's name, hold for confirmation or merge.

    Without ``confirm_merge`` a collision changes nothing and returns
    ``requires_confirmation`` with the colliding tag. With it, the renamed tag
    is merged into that one. A new spelling of the tag's own name is a plain rename.

    Raises:
        InvalidTagNameError: The new name is empty once normalized.
        TagNotFoundError: The tag no longer exists.
        HoldRefusedError: A confirmed merge would re-point a held file's tag.
    """
    cleaned = clean_tag_name(new_name)
    normalized = normalize_tag_name(cleaned)
    if not normalized:
        raise InvalidTagNameError("Tag name is required")
    tag = lock_tags(db, [tag_id]).get(tag_id)
    if tag is None:
        raise TagNotFoundError(TAG_NOT_FOUND)

    existing = _rename_collision(db, tag, normalized, cleaned, user_id)
    if existing is not None and existing.id != tag.id:
        if confirm_merge:
            return merge_tags(
                db, existing.id, [tag.id], user_id=user_id, organization_id=organization_id
            )
        impact = preview_tag_impact(db, [tag.id], user_id=user_id, organization_id=organization_id)
        return TagMutationOutcome(impact=impact, tag=existing, requires_confirmation=True)

    impact = preview_tag_impact(db, [tag.id], user_id=user_id, organization_id=organization_id)
    affected = _file_ids_of(db, [tag.id])
    old_form = stored_normalized_name(tag)
    was_system = is_system_tag(tag)
    tag.name = cleaned
    tag.normalized_name = normalized
    rewrite_stored_tag_names(db, {old_form}, cleaned, tenant=tenant_key(tag))
    db.flush()
    db.commit()
    on_tags_changed(db, affected, user_id=user_id, system_scope=was_system)
    return TagMutationOutcome(impact=impact, tag=tag)


def delete_tags(
    db: Session,
    tag_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: int | None = None,
) -> TagMutationOutcome:
    """Delete tags, their associations and (by cascade) their share grants.

    The impact is measured before anything is removed. Stored watch-source
    names of each tag's tenant are stripped.

    Raises:
        TagNotFoundError: Any of the tags no longer exists.
        HoldRefusedError: A tag is on a file under a legal hold.
    """
    if not tag_ids:
        return TagMutationOutcome(impact=TagImpactReport())
    ids = sorted({int(i) for i in tag_ids})
    tags = _require_all(lock_tags(db, ids), ids)
    impact = preview_tag_impact(db, ids, user_id=user_id, organization_id=organization_id)
    affected = _file_ids_of(db, ids)
    # Deleting a tag a held file carries would change that file's preserved record.
    assert_content_mutable(db, affected)
    deleted_uuids = [tag.uuid for tag in tags]
    system_scope = touches_system_tag(tags)

    by_tenant: dict[tuple[Any, ...], set[str]] = {}
    for tag in tags:
        by_tenant.setdefault(tenant_key(tag), set()).add(stored_normalized_name(tag))
    for tenant, names in by_tenant.items():
        rewrite_stored_tag_names(db, names, None, tenant=tenant)
    db.execute(delete(FileTag).where(FileTag.tag_id.in_(ids)))
    for tag in tags:
        db.delete(tag)
    db.commit()
    on_tags_changed(db, affected, user_id=user_id, system_scope=system_scope)
    return TagMutationOutcome(impact=impact, deleted_uuids=deleted_uuids)


def cleanup_unreferenced_tags(
    db: Session,
    *,
    acting_user_id: int,
    organization_id: int | None = None,
    all_users: bool = False,
) -> int:
    """Delete tags no file carries anywhere. System tags are always kept.

    "Unreferenced" is deployment-wide: a tag on a file the caller can no longer
    open still has a user and survives.

    Args:
        db: Session.
        acting_user_id: The caller.
        organization_id: The request tenant (None = personal).
        all_users: Sweep every account's tags, not only the caller's own in
            the request tenant.

    Returns:
        The number of tags the delete statement removed.
    """
    conditions: list[Any] = [
        ~exists().where(FileTag.tag_id == Tag.id),
        not_(system_tag()),
    ]
    if not all_users:
        conditions.append(Tag.user_id == acting_user_id)
        conditions.append(
            Tag.organization_id == organization_id
            if organization_id is not None
            else Tag.organization_id.is_(None)
        )
    result = db.execute(
        delete(Tag).where(*conditions).execution_options(synchronize_session="fetch")
    )
    deleted = int(getattr(result, "rowcount", 0) or 0)
    if not deleted:
        return 0
    db.commit()
    logger.info(f"Removed {deleted} unreferenced tags (all_users={all_users})")
    on_tags_changed(db, user_id=acting_user_id, system_scope=all_users)
    return deleted


def _same_tenant_as(key: tuple[Any, ...]) -> Any:
    """SQL predicate for the non-system tenant named by a ``tenant_key``."""
    if key[0] == "org":
        return Tag.organization_id == key[1]
    return and_(Tag.user_id == key[1], Tag.organization_id.is_(None))


def promote_tags_to_shared(
    db: Session,
    tag_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: int | None = None,
    fold_across_tenants: bool = True,
) -> TagMutationOutcome:
    """Move tags into the system vocabulary, folding same-named rows into each.

    Requested tags are processed in ascending id order. A tag whose normalized
    name already has a system row (or was promoted earlier in this call) is
    folded into it; otherwise it becomes the system row and every other tag of
    that name is folded into it, restricted to the tag's own tenant unless
    ``fold_across_tenants``. The whole call commits once, or not at all.

    Raises:
        TagNotFoundError: A requested tag (or a chosen survivor) no longer exists.
        HoldRefusedError: A fold would re-point a tag on a file under a legal hold.
    """
    if not tag_ids:
        return TagMutationOutcome(impact=TagImpactReport())
    ids = sorted({int(i) for i in tag_ids})
    requested = _require_all(lock_tags(db, ids), ids)
    pending = [tag for tag in requested if not is_system_tag(tag)]
    if not pending:
        impact = preview_tag_impact(db, ids, user_id=user_id, organization_id=organization_id)
        return TagMutationOutcome(impact=impact)

    impact = preview_tag_impact(
        db, [tag.id for tag in pending], user_id=user_id, organization_id=organization_id
    )
    survivors: dict[str, Tag] = {}
    folded: dict[int, UUID] = {}
    affected: list[int] = []
    for tag in pending:
        if tag.id in folded:
            continue
        form = stored_normalized_name(tag)
        survivor = survivors.get(form) or (
            db.query(Tag)
            .filter(system_tag(), Tag.normalized_name == form, Tag.id != tag.id)
            .order_by(Tag.id)
            .first()
        )
        if survivor is not None:
            doomed_ids = [tag.id]
        else:
            origin = tenant_key(tag)
            affected.extend(_file_ids_of(db, [tag.id]))
            tag.user_id = None
            tag.organization_id = None
            survivor = tag
            same_name = db.query(Tag.id).filter(Tag.normalized_name == form, Tag.id != tag.id)
            if not fold_across_tenants:
                same_name = same_name.filter(_same_tenant_as(origin))
            doomed_ids = sorted(int(row_id) for (row_id,) in same_name)
        survivors[form] = survivor
        if not doomed_ids:
            continue
        locked = lock_tags(db, [survivor.id, *doomed_ids])
        if survivor.id not in locked:
            raise TagNotFoundError(TAG_NOT_FOUND)
        doomed = [locked[i] for i in doomed_ids if i in locked]
        folded.update((row.id, row.uuid) for row in doomed)
        affected.extend(_fold(db, survivor, doomed))

    db.flush()
    db.commit()
    on_tags_changed(db, list(dict.fromkeys(affected)), user_id=user_id, system_scope=True)
    return TagMutationOutcome(impact=impact, deleted_uuids=[folded[i] for i in sorted(folded)])
