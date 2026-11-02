"""Tag names: normalization, resolution to a row, creation, and the change hook.

Resolution is **normalized-exact** inside one tenant plus the system
vocabulary: ``"Q3_Review"`` and ``"q3 review"`` are the same tag, while a near
match such as ``"q4 review"`` is a different one. Fuzzy similarity exists only
as an explicit suggestion (:func:`suggest_similar_tag`); it never resolves.

Nothing here commits. Callers own the transaction and call
:func:`on_tags_changed` once their change has committed.
"""

from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.constants import FUZZY_MATCH_THRESHOLD
from app.core.constants import TAG_SOURCE_MANUAL
from app.core.exceptions import OpenTranscribeError
from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.tag_scope import owned_or_system
from app.services.tag_scope import tenant_bound_or_system
from app.services.tag_scope import tenant_first

logger = logging.getLogger(__name__)

#: Width of ``tag.name``; longer input is clamped to this many characters.
MAX_TAG_NAME_LENGTH = 50

# Kept byte-for-byte in step with frontend/src/lib/utils/tagName.ts.
_SEPARATOR_RUN = re.compile(r"[-_]+")
_WHITESPACE_RUN = re.compile(r"\s+")


class InvalidTagNameError(OpenTranscribeError):
    """A supplied tag name is empty once normalized."""


def normalize_tag_name(name: str) -> str:
    """Return the comparison form of a tag name.

    Lowercases, turns each run of ``-``/``_`` into a space, collapses
    whitespace and trims. Nothing else is altered.

    Args:
        name: Any user-supplied spelling.

    Returns:
        The normalized form; ``""`` for empty input or a name made only of
        separators and whitespace.
    """
    if not name:
        return ""
    collapsed = _SEPARATOR_RUN.sub(" ", name.lower())
    return _WHITESPACE_RUN.sub(" ", collapsed).strip()


def clean_tag_name(name: str) -> str:
    """Return the display form stored in ``tag.name``.

    Trims, clamps to :data:`MAX_TAG_NAME_LENGTH` characters and trims again so
    a cut after a space leaves no trailing blank. Case and inner spacing are kept.
    """
    if not name:
        return ""
    return name.strip()[:MAX_TAG_NAME_LENGTH].strip()


def stored_normalized_name(tag: Any) -> str:
    """The tag's stored normalized form, recomputed when the column is empty."""
    return tag.normalized_name or normalize_tag_name(tag.name)


def names_are_similar(a: str, b: str, threshold: float = FUZZY_MATCH_THRESHOLD) -> bool:
    """Whether two names are equal once normalized, or close by ``SequenceMatcher`` ratio."""
    norm_a, norm_b = normalize_tag_name(a), normalize_tag_name(b)
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return True
    return difflib.SequenceMatcher(None, norm_a, norm_b).ratio() >= threshold


def tags_by_uuid(db: Session, tag_uuids: Iterable[UUID]) -> dict[UUID, Tag]:
    """Load tags by public uuid in one query; unknown uuids are absent."""
    wanted = set(tag_uuids)
    if not wanted:
        return {}
    return {tag.uuid: tag for tag in db.query(Tag).filter(Tag.uuid.in_(wanted))}


def lookup_existing_tag(
    db: Session, normalized: str, name: str, user_id: int, organization_id: int | None
) -> Tag | None:
    """Find the row a name resolves to in one tenant plus the system vocabulary.

    Matches the stored normalized form first. Failing that, matches the exact
    stored ``name``, which catches legacy rows whose normalization is missing
    or stale; a missing one is filled in and flushed (not committed).

    Args:
        db: Session.
        normalized: ``normalize_tag_name`` of the cleaned name.
        name: The cleaned display spelling.
        user_id: The caller.
        organization_id: The tenant to resolve in (None = personal).

    Returns:
        The tenant's row ahead of a same-named system row, or None.
    """
    scope = owned_or_system(user_id, organization_id)
    tag: Tag | None = (
        db.query(Tag)
        .filter(scope, Tag.normalized_name == normalized)
        .order_by(*tenant_first(), Tag.id)
        .first()
    )
    if tag is not None:
        return tag
    tag = db.query(Tag).filter(scope, Tag.name == name).order_by(*tenant_first(), Tag.id).first()
    if tag is not None and not tag.normalized_name:
        tag.normalized_name = normalized
        db.flush()
    return tag


def lookup_tag_on_file(
    db: Session, normalized: str, file_id: int, organization_id: int | None
) -> Tag | None:
    """Find a tag already on ``file_id`` with this normalized form.

    Only rows the file's tenant may carry count (that tenant's tags, or system
    tags), so a foreign tag left on the file by legacy data is never reused.
    """
    tag: Tag | None = (
        db.query(Tag)
        .join(FileTag, FileTag.tag_id == Tag.id)
        .filter(
            FileTag.media_file_id == file_id,
            Tag.normalized_name == normalized,
            tenant_bound_or_system(organization_id),
        )
        .order_by(*tenant_first(), Tag.id)
        .first()
    )
    return tag


def suggest_similar_tag(
    db: Session,
    name: str,
    *,
    user_id: int,
    organization_id: int | None,
    threshold: float = FUZZY_MATCH_THRESHOLD,
    candidates: list[Tag] | None = None,
) -> Tag | None:
    """Return the first tag in the tenant whose name is close to ``name``.

    A suggestion only: human-driven paths must show it, never apply it.

    Args:
        db: Session.
        name: The name to match.
        user_id: The caller.
        organization_id: The tenant (None = personal).
        threshold: Minimum ``SequenceMatcher`` ratio.
        candidates: A pre-loaded pool, which the caller guarantees was drawn
            from the same tenant; loaded here when omitted.
    """
    if not normalize_tag_name(name):
        return None
    pool = candidates
    if pool is None:
        pool = (
            db.query(Tag)
            .filter(owned_or_system(user_id, organization_id))
            .order_by(*tenant_first(), Tag.id)
            .all()
        )
    return next((tag for tag in pool if names_are_similar(name, tag.name, threshold)), None)


def _prepare_name(name: str | None) -> tuple[str, str]:
    cleaned = clean_tag_name(name or "")
    return cleaned, normalize_tag_name(cleaned)


def _insert_tag(
    db: Session,
    cleaned: str,
    normalized: str,
    *,
    user_id: int,
    organization_id: int | None,
    source: str,
) -> Tag:
    """Create the row in a savepoint; on a lost race return the winning row.

    The insert must go through the ORM: the race tests inject a competing
    commit from a ``before_flush`` hook that looks for this row in ``session.new``.

    Raises:
        IntegrityError: The insert collided and no winning row is visible.
    """
    savepoint = db.begin_nested()
    tag = Tag(
        name=cleaned,
        user_id=user_id,
        organization_id=organization_id,
        source=source,
        normalized_name=normalized,
    )
    try:
        db.add(tag)
        db.flush()
    except IntegrityError:
        # Only the savepoint goes: the caller's other pending writes must survive.
        savepoint.rollback()
        winner = lookup_existing_tag(db, normalized, cleaned, user_id, organization_id)
        if winner is None:
            raise
        return winner
    savepoint.commit()
    return tag


def resolve_or_create_tag(
    db: Session,
    name: str,
    *,
    user_id: int,
    organization_id: int | None,
    source: str = TAG_SOURCE_MANUAL,
    file_id: int | None = None,
) -> Tag:
    """Resolve ``name`` to a tag in the given tenant, creating it when absent.

    Args:
        db: Session. Flushed, never committed.
        name: The supplied spelling.
        user_id: The caller; recorded as owner (or creator) of a new row.
        organization_id: The tenant to resolve and create in (None = personal).
        source: Origin recorded on a newly created row.
        file_id: When given, a matching tag already on that file wins, so two
            people tagging one shared file do not fork the same word.

    Returns:
        A flushed ``Tag`` row. A new row is never a system tag.

    Raises:
        InvalidTagNameError: The name is empty once normalized.
        IntegrityError: The insert collided and no winning row could be found.
    """
    cleaned, normalized = _prepare_name(name)
    if not normalized:
        raise InvalidTagNameError("Tag name is required")
    if file_id is not None:
        on_file = lookup_tag_on_file(db, normalized, file_id, organization_id)
        if on_file is not None:
            return on_file
    existing = lookup_existing_tag(db, normalized, cleaned, user_id, organization_id)
    if existing is not None:
        return existing
    created = _insert_tag(
        db,
        cleaned,
        normalized,
        user_id=user_id,
        organization_id=organization_id,
        source=source,
    )
    return created


def resolve_or_create_tags(
    db: Session,
    names: Iterable[str],
    *,
    user_id: int,
    organization_id: int | None,
    source: str = TAG_SOURCE_MANUAL,
) -> list[Tag]:
    """Batched :func:`resolve_or_create_tag` with a constant number of SELECTs.

    Blank names are dropped; spellings with one normalized form collapse onto
    the first. The exact-name fallback repairs only an empty stored
    normalization. A name whose insert loses a race and still cannot be found
    is logged and skipped.

    Returns:
        Tags in first-seen order of their normalized form.
    """
    wanted: dict[str, str] = {}
    for raw in names:
        cleaned, normalized = _prepare_name(raw)
        if normalized and normalized not in wanted:
            wanted[normalized] = cleaned
    if not wanted:
        return []

    scope = owned_or_system(user_id, organization_id)
    found: dict[str, Tag] = {}
    by_normalized = (
        db.query(Tag)
        .filter(scope, Tag.normalized_name.in_(list(wanted)))
        .order_by(*tenant_first(), Tag.id)
        .all()
    )
    for tag in by_normalized:
        found.setdefault(tag.normalized_name or "", tag)

    missing = [norm for norm in wanted if norm not in found]
    if missing:
        by_name = (
            db.query(Tag)
            .filter(scope, Tag.name.in_([wanted[norm] for norm in missing]))
            .order_by(*tenant_first(), Tag.id)
            .all()
        )
        for tag in by_name:
            norm = normalize_tag_name(tag.name)
            if norm in wanted and norm not in found:
                found[norm] = tag
                if not tag.normalized_name:
                    tag.normalized_name = norm

    resolved: list[Tag] = []
    for norm, cleaned in wanted.items():
        hit = found.get(norm)
        if hit is not None:
            resolved.append(hit)
            continue
        try:
            resolved.append(
                _insert_tag(
                    db,
                    cleaned,
                    norm,
                    user_id=user_id,
                    organization_id=organization_id,
                    source=source,
                )
            )
        except IntegrityError:
            logger.warning(f"Tag '{cleaned}' lost a creation race and could not be re-read")
    return resolved


def on_tags_changed(
    db: Session,
    file_ids: Iterable[int] | None = None,
    *,
    user_id: int | None = None,
    system_scope: bool = False,
) -> list[int]:
    """Propagate a committed tag change to the read cache and the search index.

    Both steps are best-effort: the change has already committed, and a cache
    or broker outage must not turn it into an error.

    Args:
        db: Session, used to look up the owners of the affected files.
        file_ids: Files whose tag set (or a tag's name) changed.
        user_id: The acting user, whose cached tag list is dropped too.
        system_scope: The change touched a system tag, which every account
            lists, so every user's cached tag list is dropped.

    Returns:
        The affected file ids, deduplicated in first-seen order.
    """
    affected = list(dict.fromkeys(int(fid) for fid in (file_ids or []) if fid is not None))

    try:
        from app.services.redis_cache_service import redis_cache

        if system_scope:
            redis_cache.invalidate_tags_global()
        users: set[int] = set()
        if affected:
            owners = db.query(MediaFile.user_id).filter(MediaFile.id.in_(affected)).distinct()
            users.update(int(owner) for (owner,) in owners if owner is not None)
        if user_id is not None:
            users.add(int(user_id))
        for uid in sorted(users):
            redis_cache.invalidate_tags(uid)
            redis_cache.invalidate_user_files(uid)
    except Exception as e:
        logger.debug(f"Tag cache invalidation failed (non-critical): {e}")

    if affected:
        try:
            # Looked up at call time so the task attribute can be replaced in tests.
            from app.tasks import search_file_field_tasks

            search_file_field_tasks.update_file_tags_index.delay(affected)
        except Exception as e:
            logger.warning(f"Could not enqueue the tag reindex for {len(affected)} files: {e}")
    return affected
