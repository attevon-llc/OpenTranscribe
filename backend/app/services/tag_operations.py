"""Tag management operations: impact preview, rename, merge, delete.

Split out of :mod:`app.services.tag_service`, which owns *resolution* (one
supplied name → one ``Tag`` row). This module owns the destructive side, and it
carries a different set of hazards:

* **A tag reaches files the caller cannot see.** Since ``v374_add_tag_user_id``
  a tag is owned (``Tag.user_id``) or *system* (``user_id IS NULL``), and a
  system tag is attached across every account. ``GET /tags`` reports counts
  scoped to what the caller can see, so a confirmation reading "3 files" in
  front of a delete that strips the tag from 500 is a lie:
  :func:`preview_tag_impact` reports the caller-visible count and the true
  deployment-wide count as **separate** numbers. Which tags a caller may reach
  at all is decided before this module runs, by
  ``endpoints/tags.py:_writable_tag_ids`` — own tags always, system tags for an
  admin, never another account's.
* **``file_tag`` carries ``UNIQUE(media_file_id, tag_id)``** (declared in the DDL
  at ``alembic/versions/v010_baseline.py``, and now on the ORM model too). A bare
  ``UPDATE file_tag SET tag_id = survivor`` therefore aborts the transaction the
  first time a file carries both tags — which is the common case for exactly the
  near-duplicate pairs merge exists for. Merge collapses those pairs instead.
* **NULL origins are legacy, not unknown.** ``Tag.source``/``FileTag.source`` are
  nullable and migration v230 backfilled ``normalized_name`` but not ``source``,
  so every tag predating auto-labeling carries NULL. NULL orders as manual.
* **Tag names are stored outside the tag table.** ``WatchSource.tag_names`` is a
  JSON list of names reapplied on every poll; merge a tag away without rewriting
  it and the next poll recreates the tag.

Every mutation here commits and then calls
:func:`app.services.tag_service.on_tags_changed` — the reindex task reads the tag
rows back from its own session, so an uncommitted change is invisible to it.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field

from sqlalchemy import distinct
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import TAG_SOURCE_AI_ACCEPTED
from app.core.constants import TAG_SOURCE_AUTO_AI
from app.core.constants import TAG_SOURCE_MANUAL
from app.core.exceptions import OpenTranscribeError
from app.models.media import FileTag
from app.models.media import Tag
from app.models.watch_source import WatchSource
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import accessible_file_ids_subquery
from app.services.tag_service import clean_tag_name
from app.services.tag_service import lookup_existing_tag
from app.services.tag_service import normalize_tag_name
from app.services.tag_service import on_tags_changed
from app.services.tag_service import stored_normalized_name

logger = logging.getLogger(__name__)


class TagNotFoundError(OpenTranscribeError):
    """A tag referenced by an operation no longer exists.

    Raised rather than silently skipped: two merges running in opposite
    directions must not both report success, or each destroys the other's
    survivor.
    """


#: Origin ordering, least human first. A NULL origin predates auto-labeling and
#: is therefore human-entered, so it ranks with ``manual``.
_ORIGIN_RANK: dict[str, int] = {
    TAG_SOURCE_AUTO_AI: 0,
    TAG_SOURCE_AI_ACCEPTED: 1,
    TAG_SOURCE_MANUAL: 2,
}
_MANUAL_RANK = _ORIGIN_RANK[TAG_SOURCE_MANUAL]


@dataclass(frozen=True)
class TagImpactEntry:
    """File counts for a single tag in a pending operation."""

    uuid: uuid_pkg.UUID
    name: str
    accessible_file_count: int
    total_file_count: int


@dataclass(frozen=True)
class TagImpactReport:
    """What a rename / merge / delete would touch, before it touches it.

    Attributes:
        tags: Per-tag breakdown, in the order the tags were supplied.
        accessible_file_count: Distinct files the caller can see across all tags.
        total_file_count: Distinct files across all tags install-wide. Always
            greater than or equal to ``accessible_file_count``; the gap is the
            damage a caller-scoped confirmation would have hidden.
    """

    tags: list[TagImpactEntry] = field(default_factory=list)
    accessible_file_count: int = 0
    total_file_count: int = 0


@dataclass
class TagMutationOutcome:
    """Result of a rename / merge / delete.

    Attributes:
        tag: The surviving tag, when one survives.
        merged: True when associations were moved onto a surviving tag.
        requires_confirmation: True when the operation was *not* applied because
            it turned out to be a merge the caller has not confirmed.
        deleted_uuids: Public identifiers of the tags that were removed.
        impact: What the operation touched (or would touch, when it was held for
            confirmation).
    """

    impact: TagImpactReport
    tag: Tag | None = None
    merged: bool = False
    requires_confirmation: bool = False
    deleted_uuids: list[uuid_pkg.UUID] = field(default_factory=list)


def _origin_rank(source: str | None) -> int:
    """Rank an origin by how human it is; NULL and unknown values read as manual."""
    if source is None:
        return _MANUAL_RANK
    return _ORIGIN_RANK.get(source, _MANUAL_RANK)


def _promote_origin(keep: str | None, other: str | None) -> str | None:
    """Return the more human of two association origins.

    A NULL that outranks a machine origin is written out as ``manual`` — the
    promotion is real, and storing it repairs a legacy row on the way past. Two
    NULLs stay NULL, so an untouched pair is left exactly as it was.
    """
    if _origin_rank(other) > _origin_rank(keep):
        return other if other is not None else TAG_SOURCE_MANUAL
    return keep


def _better_confidence(a: float | None, b: float | None) -> float | None:
    """Return the higher of two optional confidences."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def lock_tags(db: Session, tag_ids: Iterable[int]) -> dict[int, Tag]:
    """Lock the given tag rows ``FOR UPDATE`` in ascending id order.

    The ordering is the point: two merges running in opposite directions
    (``A → B`` and ``B → A``) that grabbed their rows in caller order would
    deadlock or interleave into each destroying the other's survivor.

    Args:
        db: Database session.
        tag_ids: Tag ids to lock. Missing ids are simply absent from the result.

    Returns:
        The locked rows keyed by id.
    """
    wanted = sorted({int(tag_id) for tag_id in tag_ids})
    if not wanted:
        return {}
    rows = db.query(Tag).filter(Tag.id.in_(wanted)).order_by(Tag.id).with_for_update().all()
    return {row.id: row for row in rows}


def _affected_file_ids(db: Session, tag_ids: Sequence[int]) -> list[int]:
    """Return the distinct files carrying any of these tags, for the reindex hook.

    The null guard is not decorative: ``file_tag.media_file_id`` is nullable, and
    a ``None`` in the list would reach the search-refresh task as a file id.

    Args:
        db: Database session.
        tag_ids: Tags whose associations are about to change.

    Returns:
        The distinct, non-null media file ids.
    """
    return [
        file_id
        for (file_id,) in db.query(FileTag.media_file_id)
        .filter(FileTag.tag_id.in_(list(tag_ids)))
        .distinct()
        .all()
        if file_id is not None
    ]


def preview_tag_impact(
    db: Session,
    tag_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: int | None = None,
    association_source: str | None = None,
) -> TagImpactReport:
    """Count the files a pending tag operation would affect.

    Errors are allowed to propagate — an impact preview that degrades to zero
    would understate a destructive operation, which is the exact failure this
    exists to prevent.

    Args:
        db: Database session.
        tag_ids: Tags the operation covers.
        user_id: The acting user, used to scope the accessible count.
        organization_id: Tenant scope for the accessible count (``None`` =
            personal), matching ``GET /tags``.
        association_source: Count only files whose association carries this
            origin. Rename / merge / delete act on every association and leave
            it ``None``; reject (``app.services.tag_review``) removes only the
            auto-labeler's own associations, so counting them all would
            overstate what it takes away. A legacy NULL association origin never
            matches — NULL is a human's row.

    Returns:
        Per-tag and union counts, accessible and global reported separately.
    """
    ordered = [int(tag_id) for tag_id in tag_ids]
    if not ordered:
        return TagImpactReport()

    tags = {tag.id: tag for tag in db.query(Tag).filter(Tag.id.in_(ordered)).all()}
    accessible_sq = select(accessible_file_ids_subquery(db, user_id, organization_id))
    scope = [FileTag.tag_id.in_(ordered)]
    if association_source is not None:
        scope.append(FileTag.source == association_source)

    total_by_tag: dict[int, int] = {
        int(tag_id): int(count)
        for tag_id, count in db.query(FileTag.tag_id, func.count(distinct(FileTag.media_file_id)))
        .filter(*scope)
        .group_by(FileTag.tag_id)
        .all()
        if tag_id is not None
    }
    accessible_by_tag: dict[int, int] = {
        int(tag_id): int(count)
        for tag_id, count in db.query(FileTag.tag_id, func.count(distinct(FileTag.media_file_id)))
        .filter(*scope, FileTag.media_file_id.in_(accessible_sq))
        .group_by(FileTag.tag_id)
        .all()
        if tag_id is not None
    }

    entries = [
        TagImpactEntry(
            uuid=tags[tag_id].uuid,
            name=tags[tag_id].name,
            accessible_file_count=int(accessible_by_tag.get(tag_id, 0)),
            total_file_count=int(total_by_tag.get(tag_id, 0)),
        )
        for tag_id in ordered
        if tag_id in tags
    ]

    # Union counts, not the sum of the per-tag numbers: a file carrying two of
    # the tags is one affected file, not two.
    union_total = db.query(func.count(distinct(FileTag.media_file_id))).filter(*scope).scalar() or 0
    union_accessible = (
        db.query(func.count(distinct(FileTag.media_file_id)))
        .filter(*scope, FileTag.media_file_id.in_(accessible_sq))
        .scalar()
        or 0
    )

    return TagImpactReport(
        tags=entries,
        accessible_file_count=int(union_accessible),
        total_file_count=int(union_total),
    )


def rewrite_stored_tag_names(
    db: Session, doomed_normalized: set[str], replacement: str | None
) -> int:
    """Rewrite tag names stored outside the tag table.

    ``WatchSource.tag_names`` holds *names*, reapplied on every poll, so a merge
    that ignores it is undone by the next scan. Matching is on the normalized
    form so entries written before normalization existed are still caught.

    The rewritten list is **reassigned wholesale**: the column is plain ``JSON``
    with no ``MutableList`` wrapper, so mutating an element in place passes its
    own assertion in-session and is silently dropped on commit.

    Args:
        db: Database session.
        doomed_normalized: Normalized names to rewrite or strip.
        replacement: Name to substitute, or ``None`` to strip the entry.

    Returns:
        The number of watch sources changed.
    """
    if not doomed_normalized:
        return 0

    replacement_normalized = normalize_tag_name(replacement) if replacement else ""
    changed = 0

    for source in db.query(WatchSource).filter(WatchSource.tag_names.isnot(None)).all():
        stored = source.tag_names
        if not isinstance(stored, list):
            continue

        rewritten: list = []
        seen: set[str] = set()
        modified = False

        for entry in stored:
            if not isinstance(entry, str):
                rewritten.append(entry)
                continue

            entry_normalized = normalize_tag_name(entry)
            if entry_normalized in doomed_normalized:
                modified = True
                if replacement is None:
                    continue
                candidate, candidate_normalized = replacement, replacement_normalized
            else:
                candidate, candidate_normalized = entry, entry_normalized

            if candidate_normalized and candidate_normalized in seen:
                # The survivor was already configured on this source.
                modified = True
                continue
            if candidate_normalized:
                seen.add(candidate_normalized)
            rewritten.append(candidate)

        if modified:
            source.tag_names = rewritten
            changed += 1

    if changed:
        db.flush()
    return changed


def _reassign_associations(db: Session, target: Tag, doomed_ids: Sequence[int]) -> None:
    """Move every association off the doomed tags onto the survivor.

    ``file_tag`` carries ``UNIQUE(media_file_id, tag_id)``, so a file that
    already holds the survivor cannot simply have its second row repointed —
    that is the ``IntegrityError`` a bare bulk ``UPDATE`` hits on the first
    genuinely-duplicated pair. Those associations are collapsed instead: one row
    survives, carrying the more human origin and the higher confidence of the
    two.

    Args:
        db: Database session.
        target: The surviving tag.
        doomed_ids: Tags whose associations are being moved.
    """
    kept_by_file: dict[int, FileTag] = {
        link.media_file_id: link
        for link in db.query(FileTag).filter(FileTag.tag_id == target.id).all()
        if link.media_file_id is not None
    }

    # Delete the collapsed rows before repointing the rest so the unique
    # constraint never sees both rows of a pair at once.
    pending_moves: list[FileTag] = []
    for link in db.query(FileTag).filter(FileTag.tag_id.in_(list(doomed_ids))).all():
        file_id = link.media_file_id
        if file_id is None:
            # Orphaned association (nullable FK); repoint it and move on.
            pending_moves.append(link)
            continue
        keeper = kept_by_file.get(file_id)
        if keeper is None:
            kept_by_file[file_id] = link
            pending_moves.append(link)
            continue
        keeper.source = _promote_origin(keeper.source, link.source)
        keeper.ai_confidence = _better_confidence(keeper.ai_confidence, link.ai_confidence)
        db.delete(link)
    db.flush()

    for link in pending_moves:
        link.tag_id = target.id
    db.flush()


def merge_tags(
    db: Session,
    target_id: int,
    source_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: int | None = None,
) -> TagMutationOutcome:
    """Merge one or more tags into a surviving tag.

    A merge is a human act, so the survivor is endorsed: an auto-labeled tag
    becomes ``ai_accepted``; a manual (or legacy NULL) tag is already human and
    is left alone.

    Args:
        db: Database session. Committed on success.
        target_id: The surviving tag.
        source_ids: Tags to fold into it. Ids equal to ``target_id`` are ignored.
        user_id: The acting user, for cache invalidation.
        organization_id: Tenant scope for the accessible impact count.

    Returns:
        The outcome, carrying the survivor and the realized impact.

    Raises:
        TagNotFoundError: The target, or any named source, no longer exists.
    """
    wanted = {int(target_id)} | {int(i) for i in source_ids}
    locked = lock_tags(db, wanted)

    target = locked.get(int(target_id))
    if target is None:
        raise TagNotFoundError(f"Tag {target_id} no longer exists")

    doomed_ids = sorted({int(i) for i in source_ids} - {int(target_id)})
    missing = [i for i in doomed_ids if i not in locked]
    if missing:
        raise TagNotFoundError(f"Tags no longer exist: {missing}")

    # Only the disappearing tags' files are counted: a file carrying just the
    # survivor is untouched, and folding it into the number would overstate the
    # operation exactly as scoping the count would understate it.
    impact = preview_tag_impact(db, doomed_ids, user_id=user_id, organization_id=organization_id)
    if not doomed_ids:
        return TagMutationOutcome(impact=impact, tag=target)

    doomed = [locked[i] for i in doomed_ids]
    affected_file_ids = _affected_file_ids(db, doomed_ids)

    _reassign_associations(db, target, doomed_ids)

    if target.source == TAG_SOURCE_AUTO_AI:
        target.source = TAG_SOURCE_AI_ACCEPTED

    rewrite_stored_tag_names(db, {stored_normalized_name(tag) for tag in doomed}, target.name)

    deleted_uuids = [tag.uuid for tag in doomed]
    for tag in doomed:
        db.delete(tag)
    db.flush()
    db.commit()

    on_tags_changed(db, affected_file_ids, user_id=user_id)
    return TagMutationOutcome(impact=impact, tag=target, merged=True, deleted_uuids=deleted_uuids)


def rename_tag(
    db: Session,
    tag_id: int,
    new_name: str,
    *,
    confirm_merge: bool = False,
    user_id: int,
    organization_id: int | None = None,
) -> TagMutationOutcome:
    """Rename a tag, routing a collision to :func:`merge_tags`.

    A new name that resolves to a *different* existing tag is a merge in
    disguise, so it is held behind an impact preview until ``confirm_merge`` is
    passed. A name that resolves only to the tag's own normalized form
    (``interview`` → ``Interview``) is a plain rename.

    Args:
        db: Database session. Committed on success.
        tag_id: The tag to rename.
        new_name: Supplied name, trimmed and clamped like every other tag path.
        confirm_merge: Apply the merge when the new name collides.
        user_id: The acting user, for cache invalidation.
        organization_id: Tenant scope for the accessible impact count.

    Returns:
        The outcome. ``requires_confirmation`` is True when nothing was applied.

    Raises:
        InvalidTagNameError: The new name is empty once normalized.
        TagNotFoundError: The tag no longer exists.
    """
    cleaned = clean_tag_name(new_name)
    normalized = normalize_tag_name(cleaned)
    if not normalized:
        raise InvalidTagNameError(f"Tag name is empty after normalization: {new_name!r}")

    locked = lock_tags(db, [tag_id])
    tag = locked.get(int(tag_id))
    if tag is None:
        raise TagNotFoundError(f"Tag {tag_id} no longer exists")

    existing = lookup_existing_tag(db, normalized, cleaned, user_id)
    if existing is not None and existing.id != tag.id:
        if not confirm_merge:
            impact = preview_tag_impact(
                db, [tag.id], user_id=user_id, organization_id=organization_id
            )
            return TagMutationOutcome(impact=impact, tag=existing, requires_confirmation=True)
        return merge_tags(
            db, existing.id, [tag.id], user_id=user_id, organization_id=organization_id
        )

    impact = preview_tag_impact(db, [tag.id], user_id=user_id, organization_id=organization_id)
    old_normalized = stored_normalized_name(tag)
    affected_file_ids = _affected_file_ids(db, [tag.id])

    tag.name = cleaned
    tag.normalized_name = normalized
    rewrite_stored_tag_names(db, {old_normalized}, cleaned)
    db.flush()
    db.commit()

    on_tags_changed(db, affected_file_ids, user_id=user_id)
    return TagMutationOutcome(impact=impact, tag=tag)


def delete_tags(
    db: Session,
    tag_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: int | None = None,
) -> TagMutationOutcome:
    """Delete one or more tags, reporting what the delete touched.

    The impact is measured before any row is removed, so the returned counts
    describe the files that actually lost the tag.

    Args:
        db: Database session. Committed on success.
        tag_ids: Tags to remove.
        user_id: The acting user, for cache invalidation.
        organization_id: Tenant scope for the accessible impact count.

    Returns:
        The outcome, carrying the realized impact and the removed uuids.

    Raises:
        TagNotFoundError: Any named tag no longer exists.
    """
    wanted = sorted({int(tag_id) for tag_id in tag_ids})
    if not wanted:
        return TagMutationOutcome(impact=TagImpactReport())

    locked = lock_tags(db, wanted)
    missing = [tag_id for tag_id in wanted if tag_id not in locked]
    if missing:
        raise TagNotFoundError(f"Tags no longer exist: {missing}")

    doomed = [locked[tag_id] for tag_id in wanted]
    impact = preview_tag_impact(db, wanted, user_id=user_id, organization_id=organization_id)
    affected_file_ids = _affected_file_ids(db, wanted)

    rewrite_stored_tag_names(db, {stored_normalized_name(tag) for tag in doomed}, None)

    db.query(FileTag).filter(FileTag.tag_id.in_(wanted)).delete(synchronize_session=False)
    deleted_uuids = [tag.uuid for tag in doomed]
    for tag in doomed:
        db.delete(tag)
    db.flush()
    db.commit()

    on_tags_changed(db, affected_file_ids, user_id=user_id)
    return TagMutationOutcome(impact=impact, deleted_uuids=deleted_uuids)


def promote_tags_to_shared(
    db: Session,
    tag_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: int | None = None,
) -> TagMutationOutcome:
    """Publish owned tags into the shared vocabulary (``user_id`` → NULL).

    The consolidation half of tag management. Ownership stops one account's
    "Interview" from being renamed out from under another's, but it also lets a
    deployment accumulate one "Interview" per user with nothing to point them
    at. Promotion makes one row the canonical answer: a system tag is visible to
    every account, and ``resolve_or_create_tag`` prefers a same-named system row
    over creating a private one, so from here on a person typing "interview"
    attaches to *this* row instead of forking their own.

    Any same-named tags owned by other users are folded into the promoted row by
    :func:`merge_tags`, so promotion converges the deployment on one row rather
    than adding a fifth spelling beside four private ones. Their file
    associations carry over — nobody loses a tag they applied.

    Admin-gated at the endpoint (``_writable_tag_ids``): the result appears in
    every account's picker, so this is a deployment-level act, not a personal
    one. Already-shared tags are a no-op rather than an error, which keeps a
    repeated click idempotent.

    Args:
        db: Database session. Committed here.
        tag_ids: Tags to publish.
        user_id: The acting admin, for cache invalidation.
        organization_id: Tenant scope for the impact preview.

    Returns:
        The outcome, whose ``impact`` covers the promoted tags and everything
        merged into them.

    Raises:
        TagNotFoundError: A tag no longer exists.
    """
    wanted = sorted({int(tag_id) for tag_id in tag_ids})
    if not wanted:
        return TagMutationOutcome(impact=TagImpactReport())

    locked = lock_tags(db, wanted)
    missing = [tag_id for tag_id in wanted if tag_id not in locked]
    if missing:
        raise TagNotFoundError(f"Tags no longer exist: {missing}")

    promoted = [locked[tag_id] for tag_id in wanted if locked[tag_id].user_id is not None]
    if not promoted:
        # Every requested tag is already shared. Idempotent, not an error.
        return TagMutationOutcome(
            impact=preview_tag_impact(
                db, wanted, user_id=user_id, organization_id=organization_id
            )
        )

    # Claim the shared namespace first. `uq_tag_name_system` is UNIQUE(name)
    # WHERE user_id IS NULL, so a name already shared cannot be promoted twice —
    # fold the private row into the existing shared one instead.
    outcome_impact = preview_tag_impact(
        db, [tag.id for tag in promoted], user_id=user_id, organization_id=organization_id
    )
    affected_file_ids: list[int] = []
    for tag in promoted:
        normalized = stored_normalized_name(tag)
        already_shared = (
            db.query(Tag)
            .filter(Tag.user_id.is_(None), Tag.normalized_name == normalized, Tag.id != tag.id)
            .first()
        )
        if already_shared is not None:
            merge_tags(
                db,
                already_shared.id,
                [tag.id],
                user_id=user_id,
                organization_id=organization_id,
            )
            continue

        tag.user_id = None
        affected_file_ids.extend(_affected_file_ids(db, [tag.id]))

        # Fold every other account's same-named row into the new shared one, so
        # promotion collapses the duplicates rather than sitting beside them.
        duplicates = [
            row.id
            for row in db.query(Tag)
            .filter(Tag.normalized_name == normalized, Tag.id != tag.id)
            .all()
        ]
        if duplicates:
            merge_tags(
                db, tag.id, duplicates, user_id=user_id, organization_id=organization_id
            )

    db.flush()
    db.commit()

    # A system tag is in every account's list, so nothing narrower than a global
    # cache drop is correct here.
    on_tags_changed(db, affected_file_ids, user_id=user_id, system_scope=True)
    return TagMutationOutcome(impact=outcome_impact)
