"""Read surfaces of the tag plane: the list, usage counts, collisions, selections.

Usage is always counted over the caller's **accessible** files
(:func:`accessible_usage_counts`), so ``usage_count``, the ``unused`` filter,
``/tags/unused`` and the collision members agree on one definition.

Collisions are tags whose stored normalized names are equal. Fuzzy similarity
is not transitive, so it never makes a tag a member; near matches are offered
as ranked suggestions next to a cluster instead.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from difflib import SequenceMatcher
from typing import Final
from uuid import UUID

from sqlalchemy import distinct
from sqlalchemy import func
from sqlalchemy import not_
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import FUZZY_MATCH_THRESHOLD
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.tag_scope import accessible_file_ids_subquery
from app.services.tag_scope import owned_or_system
from app.services.tag_scope import system_tag
from app.services.tag_scope import tag_ownership
from app.services.tag_scope import visible_to
from app.services.tag_service import normalize_tag_name
from app.services.takedown_service import exclude_quarantined

logger = logging.getLogger(__name__)

SCOPE_ALL: Final = "all"
#: Values of the list's ``scope`` filter: ``all`` plus the three ownership labels.
TAG_SCOPES: Final = (SCOPE_ALL, "mine", "system", "shared_with_me")


@dataclass(frozen=True)
class TagListEntry:
    """One row of the caller's tag list."""

    uuid: UUID
    name: str
    source: str | None
    usage_count: int
    ownership: str = "mine"


@dataclass(frozen=True)
class TagClusterMember:
    """A tag inside a collision cluster."""

    uuid: UUID
    name: str
    source: str | None
    usage_count: int
    suggested_survivor: bool
    ownership: str = "mine"


@dataclass(frozen=True)
class TagClusterSuggestion:
    """A near-match tag offered beside a cluster."""

    uuid: UUID
    name: str
    source: str | None
    usage_count: int
    similarity: float
    ownership: str = "mine"


@dataclass
class TagCollisionCluster:
    """Tags sharing one normalized name, most used first."""

    normalized_name: str
    members: list[TagClusterMember] = field(default_factory=list)
    suggested_survivor_uuid: UUID | None = None
    suggestions: list[TagClusterSuggestion] = field(default_factory=list)


def refresh_stored_normalization(db: Session, *, user_id: int, organization_id: int | None) -> int:
    """Correct NULL or stale ``normalized_name`` values in the tenant and system scope.

    Commits when something changed: it runs on read requests, whose session
    closes without a commit.

    Returns:
        The number of rows corrected.
    """
    corrected = 0
    for tag in db.query(Tag).filter(owned_or_system(user_id, organization_id)):
        expected = normalize_tag_name(tag.name)
        if tag.normalized_name != expected:
            tag.normalized_name = expected
            corrected += 1
    if corrected:
        db.flush()
        db.commit()
        logger.info(f"Corrected the stored normalization of {corrected} tags")
    return corrected


def accessible_usage_counts(
    db: Session, *, user_id: int, organization_id: OrgScope = UNSCOPED
) -> dict[int, int]:
    """Distinct accessible files per tag id; tags with none are absent."""
    accessible = select(accessible_file_ids_subquery(db, user_id, organization_id))
    rows = (
        db.query(FileTag.tag_id, func.count(distinct(FileTag.media_file_id)))
        .filter(FileTag.tag_id.is_not(None), FileTag.media_file_id.in_(accessible))
        .group_by(FileTag.tag_id)
    )
    return {int(tag_id): int(count) for tag_id, count in rows}


def _scope_tags(db: Session, user_id: int, organization_id: int | None) -> list[Tag]:
    """Repair stored normalization, then load the tenant and system tags by id."""
    refresh_stored_normalization(db, user_id=user_id, organization_id=organization_id)
    return db.query(Tag).filter(owned_or_system(user_id, organization_id)).order_by(Tag.id).all()


def _group_collisions(tags: list[Tag]) -> dict[str, list[Tag]]:
    groups: dict[str, list[Tag]] = {}
    for tag in tags:
        if tag.normalized_name:
            groups.setdefault(tag.normalized_name, []).append(tag)
    return {form: members for form, members in groups.items() if len(members) > 1}


def _similarity(a: str, b: str, threshold: float) -> float | None:
    """``SequenceMatcher(None, a, b).ratio()`` when it reaches ``threshold``, else None."""
    matcher = SequenceMatcher(None, a, b)
    # The quick bounds are cheap upper limits on ratio(); most pairs stop here.
    if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
        return None
    ratio = matcher.ratio()
    return ratio if ratio >= threshold else None


def find_tag_collisions(
    db: Session,
    *,
    user_id: int,
    organization_id: int | None,
    threshold: float = FUZZY_MATCH_THRESHOLD,
) -> list[TagCollisionCluster]:
    """Cluster the tenant's and system tags by stored normalized name.

    Runs :func:`refresh_stored_normalization` first, so this may commit.
    Clusters are ordered by normalized name and members by usage, then name,
    then id; the first member is the suggested survivor. The result is
    identical across calls on unchanged data.
    """
    tags = _scope_tags(db, user_id, organization_id)
    groups = _group_collisions(tags)
    if not groups:
        return []
    usage = accessible_usage_counts(db, user_id=user_id, organization_id=organization_id)

    def rank(tag: Tag) -> tuple[int, str, int]:
        return (-usage.get(tag.id, 0), tag.name, tag.id)

    clusters: list[TagCollisionCluster] = []
    for form in sorted(groups):
        members = sorted(groups[form], key=rank)
        member_ids = {tag.id for tag in members}
        scored: list[tuple[float, Tag]] = []
        for candidate in tags:
            other = candidate.normalized_name
            if candidate.id in member_ids or not other or other == form:
                continue
            ratio = _similarity(form, other, threshold)
            if ratio is not None:
                scored.append((ratio, candidate))
        scored.sort(key=lambda pair: (-pair[0], *rank(pair[1])))
        clusters.append(
            TagCollisionCluster(
                normalized_name=form,
                members=[
                    TagClusterMember(
                        uuid=tag.uuid,
                        name=tag.name,
                        source=tag.source,
                        usage_count=usage.get(tag.id, 0),
                        suggested_survivor=index == 0,
                        ownership=tag_ownership(tag, user_id),
                    )
                    for index, tag in enumerate(members)
                ],
                suggested_survivor_uuid=members[0].uuid,
                suggestions=[
                    TagClusterSuggestion(
                        uuid=tag.uuid,
                        name=tag.name,
                        source=tag.source,
                        usage_count=usage.get(tag.id, 0),
                        similarity=round(ratio, 4),
                        ownership=tag_ownership(tag, user_id),
                    )
                    for ratio, tag in scored
                ],
            )
        )
    return clusters


def colliding_tag_ids(db: Session, *, user_id: int, organization_id: int | None) -> set[int]:
    """Ids of every tag that belongs to some collision cluster (may commit a repair)."""
    groups = _group_collisions(_scope_tags(db, user_id, organization_id))
    return {tag.id for members in groups.values() for tag in members}


def list_tags_filtered(
    db: Session,
    *,
    user_id: int,
    organization_id: int | None,
    unused: bool = False,
    colliding: bool = False,
    scope: str = SCOPE_ALL,
) -> list[TagListEntry]:
    """The caller's tag list: every tag :func:`visible_to` them, optionally narrowed.

    Args:
        db: Session.
        user_id: The caller.
        organization_id: The request tenant (None = personal).
        unused: Keep only tags on none of the caller's accessible files.
        colliding: Keep only collision-cluster members (commits the
            normalization repair when there is one).
        scope: One of :data:`TAG_SCOPES`; each ownership value keeps exactly
            the rows that report it.

    Returns:
        Entries ordered by usage descending, then name.

    Raises:
        ValueError: ``scope`` is not one of :data:`TAG_SCOPES`.
    """
    if scope not in TAG_SCOPES:
        raise ValueError(f"Unknown tag scope: {scope}")
    # Before loading the list: the repair may commit, which would expire the rows.
    keep_ids = (
        colliding_tag_ids(db, user_id=user_id, organization_id=organization_id)
        if colliding
        else None
    )

    query = db.query(Tag).filter(visible_to(db, user_id, organization_id))
    if scope == "mine":
        query = query.filter(Tag.user_id == user_id)
    elif scope == "system":
        query = query.filter(system_tag())
    elif scope == "shared_with_me":
        query = query.filter(not_(system_tag()), Tag.user_id.is_distinct_from(user_id))
    tags = query.all()

    usage = accessible_usage_counts(db, user_id=user_id, organization_id=organization_id)
    entries = [
        TagListEntry(
            uuid=tag.uuid,
            name=tag.name,
            source=tag.source,
            usage_count=usage.get(tag.id, 0),
            ownership=tag_ownership(tag, user_id),
        )
        for tag in tags
        if (not unused or not usage.get(tag.id)) and (keep_ids is None or tag.id in keep_ids)
    ]
    entries.sort(key=lambda entry: (-entry.usage_count, entry.name))
    return entries


def list_unused_tag_rows(db: Session, *, user_id: int, organization_id: int | None) -> list[Tag]:
    """Tenant and system tags on none of the caller's accessible files, by name.

    A tag may appear here and still be kept by ``DELETE /tags/cleanup``, which
    counts references deployment-wide.
    """
    accessible = select(accessible_file_ids_subquery(db, user_id, organization_id))
    in_use = select(FileTag.tag_id).where(
        FileTag.tag_id.is_not(None), FileTag.media_file_id.in_(accessible)
    )
    rows: list[Tag] = (
        db.query(Tag)
        .filter(owned_or_system(user_id, organization_id), Tag.id.not_in(in_use))
        .order_by(Tag.name)
        .all()
    )
    return rows


def tags_on_files(
    db: Session,
    file_ids: Sequence[int],
    *,
    user_id: int,
    organization_id: OrgScope = UNSCOPED,
) -> list[tuple[Tag, int]]:
    """Tags carried by the given files, with how many of them carry each.

    Only files the caller may open are counted, so a selection never reads tags
    off a file it could not open.

    Returns:
        ``(tag, file_count)`` pairs, most carried first, then by name.
    """
    if not file_ids:
        return []
    accessible = select(accessible_file_ids_subquery(db, user_id, organization_id))
    carried = func.count(distinct(FileTag.media_file_id))
    rows = (
        db.query(Tag, carried)
        .join(FileTag, FileTag.tag_id == Tag.id)
        .filter(FileTag.media_file_id.in_(list(file_ids)), FileTag.media_file_id.in_(accessible))
        .group_by(Tag.id)
        .order_by(carried.desc(), Tag.name)
        .all()
    )
    return [(tag, int(count)) for tag, count in rows]


def files_for_tag(
    db: Session,
    tag_id: int,
    *,
    user_id: int,
    organization_id: OrgScope = UNSCOPED,
    limit: int = 50,
    include_quarantined: bool = False,
) -> tuple[list[MediaFile], int]:
    """The accessible files carrying a tag, newest first, and their true total.

    Quarantined files are left out unless ``include_quarantined``: the caller's review
    visibility, ``quarantine_list_exempt(bypass)``, never the bare admin role.

    Returns:
        At most ``limit`` files, and the count of all matching files.
    """
    accessible = select(accessible_file_ids_subquery(db, user_id, organization_id))
    carrying = select(FileTag.media_file_id).where(FileTag.tag_id == tag_id)
    query = exclude_quarantined(
        db.query(MediaFile).filter(MediaFile.id.in_(carrying), MediaFile.id.in_(accessible)),
        include_quarantined=include_quarantined,
    )
    total = query.count()
    files = (
        query.order_by(MediaFile.upload_time.desc().nulls_last(), MediaFile.id.desc())
        .limit(limit)
        .all()
    )
    return files, int(total)
