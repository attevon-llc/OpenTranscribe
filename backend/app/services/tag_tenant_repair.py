"""Repoint ``file_tag`` rows whose tag belongs to another tenant than the file.

Since ``v420_add_tag_organization_id`` a file should only carry its own tenant's tags (or
system tags). That revision's phase 2 split every mismatch it found, but a row written
afterwards by a path that skipped the tenant rules (the file-aware resolver reused any tag
already on a file, of any tenant) can still pair a personal tag with an organization file,
or one organization's tag with another's file. The read side hides such rows
(``tag_scope.tenant_bound_or_system``) and the bulk rail refuses to create them; this pass
moves the ones that exist onto the right row, with v420 phase 2's rules:

* organization file: the organization's row with the same exact name, else a copy of the
  tag in that organization (same name, source and normalization, the original creator);
* personal file: the tag creator's personal row with that name, else a copy (an
  unattributed organization tag goes to the file's owner).

A file already carrying that row keeps one association, with the more human origin and
the higher AI confidence of the two. No tag row is ever deleted and every file keeps the
same word, so the search documents' ``tags`` arrays do not change.

Delivered as an idempotent startup pass (``app.startup.maintenance``), not an Alembic data
revision: the release branch carries revisions this branch does not, so a new revision here
would fork the ``down_revision`` chain at merge (``backend/alembic/CLAUDE.md``). Re-running
it is safe and, once the data is clean, costs one query.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field

from sqlalchemy import not_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.tag_operations import _better_confidence
from app.services.tag_operations import _promote_origin
from app.services.tag_scope import system_tag
from app.services.tag_service import on_tags_changed

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500


@dataclass
class TagTenantRepairReport:
    """What one pass changed."""

    repointed: int = 0
    collapsed: int = 0
    tags_created: int = 0
    file_ids: list[int] = field(default_factory=list)


def _find_target(db: Session, name: str, org_id: int | None, owner_id: int | None) -> Tag | None:
    query = db.query(Tag).filter(Tag.name == name)
    if org_id is not None:
        query = query.filter(Tag.organization_id == org_id)
    else:
        query = query.filter(Tag.organization_id.is_(None), Tag.user_id == owner_id)
    target: Tag | None = query.order_by(Tag.id).first()
    return target


def _tenant_row(
    db: Session, stray: Tag, file_org: int | None, file_owner: int, report: TagTenantRepairReport
) -> Tag:
    """The file tenant's row for ``stray``'s word, created when that tenant has none."""
    owner_id = stray.user_id if file_org is not None else (stray.user_id or file_owner)
    target = _find_target(db, stray.name, file_org, owner_id)
    if target is not None:
        return target
    nested = db.begin_nested()
    try:
        target = Tag(
            name=stray.name,
            source=stray.source,
            normalized_name=stray.normalized_name,
            user_id=owner_id,
            organization_id=file_org,
        )
        db.add(target)
        db.flush()
        nested.commit()
    except IntegrityError:
        # A request created the same row meanwhile (the partial unique indexes); use it.
        nested.rollback()
        target = _find_target(db, stray.name, file_org, owner_id)
        if target is None:
            raise
        return target
    report.tags_created += 1
    return target


def repair_cross_tenant_file_tags(
    db: Session, *, batch_size: int = DEFAULT_BATCH_SIZE
) -> TagTenantRepairReport:
    """Move every cross-tenant ``file_tag`` row onto its file tenant's own tag.

    Walks the stray associations in ``file_tag.id`` order, ``batch_size`` at a time,
    committing each batch, then fires one ``on_tags_changed`` for the touched files
    (cache invalidation; the indexed tag names are unchanged).

    Args:
        db: Database session. Committed once per batch.
        batch_size: Associations per batch.

    Returns:
        Counts of repointed and collapsed associations and created tags, and the ids
        of the files touched.
    """
    report = TagTenantRepairReport()
    last_id = 0
    while True:
        rows = (
            db.query(FileTag, Tag, MediaFile.organization_id, MediaFile.user_id)
            .join(Tag, FileTag.tag_id == Tag.id)
            .join(MediaFile, FileTag.media_file_id == MediaFile.id)
            .filter(
                FileTag.id > last_id,
                not_(system_tag()),
                Tag.organization_id.is_distinct_from(MediaFile.organization_id),
            )
            .order_by(FileTag.id)
            .limit(batch_size)
            .all()
        )
        if not rows:
            break
        for link, stray, file_org, file_owner in rows:
            target = _tenant_row(db, stray, file_org, int(file_owner), report)
            kept = (
                db.query(FileTag)
                .filter(FileTag.media_file_id == link.media_file_id, FileTag.tag_id == target.id)
                .first()
            )
            if kept is not None:
                kept.source = _promote_origin(kept.source, link.source)
                kept.ai_confidence = _better_confidence(kept.ai_confidence, link.ai_confidence)
                db.delete(link)
                report.collapsed += 1
            else:
                link.tag_id = target.id
                report.repointed += 1
            report.file_ids.append(int(link.media_file_id))
            db.flush()
        last_id = int(rows[-1][0].id)
        db.commit()

    if report.file_ids:
        report.file_ids = list(dict.fromkeys(report.file_ids))
        logger.info(
            "Tag tenant repair: %d association(s) repointed, %d collapsed, %d tag(s) created "
            "across %d file(s)",
            report.repointed,
            report.collapsed,
            report.tags_created,
            len(report.file_ids),
        )
        on_tags_changed(db, report.file_ids)
    return report
