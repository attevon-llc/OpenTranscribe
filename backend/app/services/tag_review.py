"""Review of auto-labeled tags: accept the tag, or reject what the labeler added.

A sibling of :mod:`app.services.tag_operations` (rename / merge / delete), kept
separate because that module is already at its size limit and because review has
its own rule, which is the one thing to get right here:

* **Reject is not "delete the tag".** ``Tag.source`` records which path created
  the row **first**, not who endorsed it, and both human write paths hardcode
  ``manual`` on the ``FileTag`` while leaving ``Tag.source`` alone. So a tag the
  auto-labeler created can carry any amount of hand-applied tagging. Reject
  therefore keys on ``FileTag.source == TAG_SOURCE_AUTO_AI`` and removes the tag
  row **only** when no association survives.
* **Accept is a tag-row change, not a file change.** It sets the origin to
  ``ai_accepted``, which is what moves the tag out of the awaiting-review set;
  associations are untouched, so no file needs reindexing — but every user's
  cached tag list does need busting.
* **NULL is manual.** ``Tag.source`` is nullable and migration v230 never
  backfilled it, so every tag predating auto-labeling carries NULL. Such a tag is
  not awaiting review and is not acceptable; a NULL *association* origin is a
  human's row and survives a reject.
* **Bulk selections are mixed.** The tag list allows multi-select across filters,
  so a selection routinely contains tags that were never the auto-labeler's.
  Those are reported as :data:`REVIEW_NOT_APPLICABLE` rather than failing the
  whole call — but a tag that has *vanished* still raises, exactly as in
  :mod:`app.services.tag_operations`.

Like every mutation there, the ones here commit and then call
:func:`app.services.tag_service.on_tags_changed`.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.constants import TAG_SOURCE_AI_ACCEPTED
from app.core.constants import TAG_SOURCE_AUTO_AI
from app.models.media import FileTag
from app.models.media import Tag
from app.services.tag_operations import TagImpactReport
from app.services.tag_operations import TagNotFoundError
from app.services.tag_operations import lock_tags
from app.services.tag_operations import preview_tag_impact
from app.services.tag_operations import rewrite_stored_tag_names
from app.services.tag_operations import touches_system_tag
from app.services.tag_service import is_awaiting_review
from app.services.tag_service import on_tags_changed
from app.services.tag_service import stored_normalized_name

logger = logging.getLogger(__name__)

#: Per-tag outcomes. ``not_applicable`` is a *reported* result, never an error:
#: a mixed multi-select must not fail because one member was hand-created.
REVIEW_ACCEPTED = "accepted"
REVIEW_REJECTED = "rejected"
REVIEW_NOT_APPLICABLE = "not_applicable"

#: Review actions, as they arrive from the API.
REVIEW_ACTION_ACCEPT = "accept"
REVIEW_ACTION_REJECT = "reject"


@dataclass(frozen=True)
class TagReviewEntry:
    """What accept / reject did (or would do) to one tag.

    Attributes:
        uuid: Public identifier of the tag.
        name: Tag name at the time of the call.
        outcome: :data:`REVIEW_ACCEPTED`, :data:`REVIEW_REJECTED`, or
            :data:`REVIEW_NOT_APPLICABLE` when the tag was not the auto-labeler's.
        removed_association_count: Associations the auto-labeler created, which a
            reject removes. Always 0 for accept, which changes no association.
        retained_association_count: Associations that survive — hand-applied and
            legacy-NULL rows. A caller cannot judge a reject without this number
            beside the removed one.
        tag_removed: True when the tag row itself goes, which happens only once
            ``retained_association_count`` is 0.
    """

    uuid: uuid_pkg.UUID
    name: str
    outcome: str
    removed_association_count: int = 0
    retained_association_count: int = 0
    tag_removed: bool = False


@dataclass
class TagReviewReport:
    """Outcome of an accept / reject, or of the preview that precedes it.

    Attributes:
        impact: File counts in the shape rename / merge / delete already use. For
            a reject it counts **only** the files that lose the tag; for an
            accept it is the reach of the tags being endorsed, which the zero
            ``removed_association_count`` marks as untouched.
        tags: Per-tag outcomes, in the order the tags were supplied.
        removed_association_count: Sum across tags.
        retained_association_count: Sum across tags.
        deleted_uuids: Tags whose rows were removed (reject only).
        applied: False for a preview, True once the operation ran.
    """

    impact: TagImpactReport
    tags: list[TagReviewEntry] = field(default_factory=list)
    removed_association_count: int = 0
    retained_association_count: int = 0
    deleted_uuids: list[uuid_pkg.UUID] = field(default_factory=list)
    applied: bool = False


def _association_split(db: Session, tag_ids: Sequence[int]) -> dict[int, tuple[int, int]]:
    """Count each tag's auto-labeler-created vs. surviving associations.

    Args:
        db: Database session.
        tag_ids: Tags to count.

    Returns:
        ``{tag_id: (auto_labeled, everything_else)}``, defaulting to ``(0, 0)``.
    """
    split: dict[int, tuple[int, int]] = {int(tag_id): (0, 0) for tag_id in tag_ids}
    if not split:
        return split

    rows = (
        db.query(FileTag.tag_id, FileTag.source, func.count(FileTag.id))
        .filter(FileTag.tag_id.in_(list(split)))
        .group_by(FileTag.tag_id, FileTag.source)
        .all()
    )
    for tag_id, source, count in rows:
        if tag_id is None:
            continue
        auto, other = split[int(tag_id)]
        if source == TAG_SOURCE_AUTO_AI:
            split[int(tag_id)] = (auto + int(count), other)
        else:
            split[int(tag_id)] = (auto, other + int(count))
    return split


def _resolve(db: Session, tag_ids: Sequence[int], *, lock: bool) -> list[Tag]:
    """Fetch the requested tags in the supplied order, raising if any has vanished.

    Args:
        db: Database session.
        tag_ids: Tags under review.
        lock: Take ``FOR UPDATE`` in id order (mutations only — a preview that
            locked would hold write locks for a read).

    Returns:
        The rows, in the order they were supplied.

    Raises:
        TagNotFoundError: Any named tag no longer exists — the same loud failure
            merge and delete make, so a stale selection cannot report success.
    """
    ordered = [int(tag_id) for tag_id in tag_ids]
    if lock:
        found = lock_tags(db, ordered)
    else:
        found = {row.id: row for row in db.query(Tag).filter(Tag.id.in_(ordered)).all()}
    missing = sorted({tag_id for tag_id in ordered if tag_id not in found})
    if missing:
        raise TagNotFoundError(f"Tags no longer exist: {missing}")
    return [found[tag_id] for tag_id in ordered]


def _build_report(
    db: Session,
    tags: Sequence[Tag],
    *,
    action: str,
    user_id: int,
    organization_id: int | None,
) -> TagReviewReport:
    """Describe what ``action`` would do to ``tags``, changing nothing."""
    split = _association_split(db, [tag.id for tag in tags])
    rejecting = action == REVIEW_ACTION_REJECT
    entries: list[TagReviewEntry] = []
    eligible_ids: list[int] = []

    for tag in tags:
        auto_count, other_count = split.get(tag.id, (0, 0))
        if not is_awaiting_review(tag):
            entries.append(
                TagReviewEntry(
                    uuid=tag.uuid,
                    name=tag.name,
                    outcome=REVIEW_NOT_APPLICABLE,
                    retained_association_count=auto_count + other_count,
                )
            )
            continue

        eligible_ids.append(tag.id)
        removed = auto_count if rejecting else 0
        retained = other_count if rejecting else auto_count + other_count
        entries.append(
            TagReviewEntry(
                uuid=tag.uuid,
                name=tag.name,
                outcome=REVIEW_REJECTED if rejecting else REVIEW_ACCEPTED,
                removed_association_count=removed,
                retained_association_count=retained,
                tag_removed=rejecting and retained == 0,
            )
        )

    impact = preview_tag_impact(
        db,
        eligible_ids,
        user_id=user_id,
        organization_id=organization_id,
        association_source=TAG_SOURCE_AUTO_AI if rejecting else None,
    )
    return TagReviewReport(
        impact=impact,
        tags=entries,
        removed_association_count=sum(e.removed_association_count for e in entries),
        retained_association_count=sum(e.retained_association_count for e in entries),
    )


def preview_tag_review(
    db: Session,
    tag_ids: Sequence[int],
    *,
    action: str,
    user_id: int,
    organization_id: int | None = None,
) -> TagReviewReport:
    """Report what accepting or rejecting these tags would do, without doing it.

    Args:
        db: Database session. Not committed.
        tag_ids: Tags under review.
        action: :data:`REVIEW_ACTION_ACCEPT` or :data:`REVIEW_ACTION_REJECT`.
        user_id: The acting user, used to scope the accessible file count.
        organization_id: Tenant scope for the accessible count.

    Returns:
        The report, with ``applied`` False.

    Raises:
        ValueError: ``action`` is neither accept nor reject. Guessing would
            report an accept's numbers in front of a reject.
        TagNotFoundError: Any named tag no longer exists.
    """
    if action not in (REVIEW_ACTION_ACCEPT, REVIEW_ACTION_REJECT):
        raise ValueError(f"Unknown tag review action: {action!r}")
    if not tag_ids:
        return TagReviewReport(impact=TagImpactReport())
    tags = _resolve(db, tag_ids, lock=False)
    return _build_report(db, tags, action=action, user_id=user_id, organization_id=organization_id)


def accept_tags(
    db: Session,
    tag_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: int | None = None,
) -> TagReviewReport:
    """Endorse auto-labeled tags, leaving every association untouched.

    Accepting sets the tag's origin to ``ai_accepted``, which is what removes it
    from the awaiting-review set. Tags that were not the auto-labeler's come back
    as :data:`REVIEW_NOT_APPLICABLE` instead of failing the call, so a mixed
    multi-select still applies to the tags it legitimately covers.

    Args:
        db: Database session. Committed on success.
        tag_ids: Tags to accept.
        user_id: The acting user, for cache invalidation.
        organization_id: Tenant scope for the accessible impact count.

    Returns:
        The report, with ``applied`` True.

    Raises:
        TagNotFoundError: Any named tag no longer exists.
    """
    if not tag_ids:
        return TagReviewReport(impact=TagImpactReport())

    tags = _resolve(db, tag_ids, lock=True)
    report = _build_report(
        db, tags, action=REVIEW_ACTION_ACCEPT, user_id=user_id, organization_id=organization_id
    )

    accepted = [tag for tag in tags if is_awaiting_review(tag)]
    system_scope = touches_system_tag(accepted)
    for tag in accepted:
        tag.source = TAG_SOURCE_AI_ACCEPTED
    db.flush()
    db.commit()

    # No file changed hands, so nothing needs reindexing — but the tag rows did
    # change, so the cached list they appear in has to go. Only a system tag
    # appears in every account's list; an owned one concerns its owner alone.
    on_tags_changed(db, user_id=user_id, system_scope=system_scope)
    logger.info("Accepted %d auto-labeled tag(s)", len(accepted))
    report.applied = True
    return report


def reject_tags(
    db: Session,
    tag_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: int | None = None,
) -> TagReviewReport:
    """Remove what the auto-labeler added, and only that.

    For each eligible tag the associations carrying ``auto_ai`` are deleted; the
    tag row itself is removed **only** when nothing is left holding it. A tag the
    labeler created but people have since applied by hand therefore survives with
    its hand-applied associations intact — deleting it would destroy work nobody
    asked to undo.

    When the row does go, its name is stripped from ``WatchSource.tag_names`` for
    the same reason a delete does it: those names are reapplied on every poll and
    would recreate the tag.

    Args:
        db: Database session. Committed on success.
        tag_ids: Tags to reject.
        user_id: The acting user, for cache invalidation.
        organization_id: Tenant scope for the accessible impact count.

    Returns:
        The report, with ``applied`` True and ``deleted_uuids`` naming the rows
        that were removed.

    Raises:
        TagNotFoundError: Any named tag no longer exists.
    """
    if not tag_ids:
        return TagReviewReport(impact=TagImpactReport())

    tags = _resolve(db, tag_ids, lock=True)
    report = _build_report(
        db, tags, action=REVIEW_ACTION_REJECT, user_id=user_id, organization_id=organization_id
    )
    by_uuid = {entry.uuid: entry for entry in report.tags}

    affected_file_ids: list[int] = []
    deleted_uuids: list[uuid_pkg.UUID] = []
    doomed_normalized: set[str] = set()

    # One select and one delete across the whole set, the same way merge and
    # delete already do it — a rejected multi-select used to issue a pair of
    # statements per tag. The rows are regrouped by tag afterwards so the
    # per-tag accounting, and the order files reach the reindex hook in, are
    # exactly what the per-tag loop produced.
    rejected = [tag for tag in tags if by_uuid[tag.uuid].outcome == REVIEW_REJECTED]
    rejected_ids = [tag.id for tag in rejected]
    # Read ownership before the deletes below — afterwards the rows are gone and
    # the answer is unavailable.
    system_scope = touches_system_tag(rejected)

    if rejected_ids:
        auto_scope = [FileTag.tag_id.in_(rejected_ids), FileTag.source == TAG_SOURCE_AUTO_AI]
        files_by_tag: dict[int, list[int]] = {}
        for tag_id, file_id in (
            db.query(FileTag.tag_id, FileTag.media_file_id).filter(*auto_scope).distinct().all()
        ):
            if tag_id is None or file_id is None:
                continue
            files_by_tag.setdefault(int(tag_id), []).append(file_id)
        for tag in rejected:
            affected_file_ids.extend(files_by_tag.get(tag.id, []))

        db.query(FileTag).filter(*auto_scope).delete(synchronize_session=False)

    for tag in rejected:
        if by_uuid[tag.uuid].tag_removed:
            doomed_normalized.add(stored_normalized_name(tag))
            deleted_uuids.append(tag.uuid)
            db.delete(tag)

    rewrite_stored_tag_names(db, doomed_normalized, None)
    db.flush()
    db.commit()

    on_tags_changed(db, affected_file_ids, user_id=user_id, system_scope=system_scope)
    logger.info(
        "Rejected %d auto-labeled tag(s); removed %d association(s), kept %d",
        len(deleted_uuids),
        report.removed_association_count,
        report.retained_association_count,
    )
    report.deleted_uuids = deleted_uuids
    report.applied = True
    return report
