"""Rewrite tag names stored outside the ``tag`` table (watch sources).

``WatchSource.tag_names`` holds plain name strings that every poll re-applies,
so a tag merged, renamed or deleted without rewriting them would be recreated
by the next poll. The column is plain JSON, not a mutable type: a change only
persists when a new list is assigned.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.tag import Tag
from app.models.watch_source import WatchSource
from app.services.tag_service import normalize_tag_name
from app.services.tag_service import stored_normalized_name


def _rewrite_list(names: list[Any], doomed: set[str], replacement: str | None) -> list[Any]:
    """Replace (or drop) doomed entries, then drop later normalized duplicates.

    Non-string entries and strings that normalize empty pass through untouched.
    """
    replacement_form = normalize_tag_name(replacement) if replacement is not None else ""
    rewritten: list[Any] = []
    seen: set[str] = set()
    for entry in names:
        if not isinstance(entry, str):
            rewritten.append(entry)
            continue
        form = normalize_tag_name(entry)
        if form in doomed:
            if replacement is None:
                continue
            entry, form = replacement, replacement_form
        if form:
            if form in seen:
                continue
            seen.add(form)
        rewritten.append(entry)
    return rewritten


def _tenant_vocabulary_filter(doomed: set[str]) -> Any:
    # Rows with no stored form are compared on their recomputed one in Python.
    return or_(
        Tag.normalized_name.in_(list(doomed)),
        Tag.normalized_name.is_(None),
        Tag.normalized_name == "",
    )


def _shadowed_names(
    db: Session, sources: list[WatchSource], doomed: set[str]
) -> dict[int, set[str]]:
    """Doomed names each source's own tenant also holds as a non-system tag.

    There the stored word means the tenant's own tag, not the system one, so a
    system-tag change must leave it alone. One query per tenant kind.
    """
    org_ids = {s.organization_id for s in sources if s.organization_id is not None}
    owner_ids = {s.user_id for s in sources if s.organization_id is None}
    held_by_org: dict[int, set[str]] = {}
    held_by_owner: dict[int, set[str]] = {}
    if org_ids:
        rows = db.query(Tag).filter(
            Tag.organization_id.in_(org_ids), _tenant_vocabulary_filter(doomed)
        )
        for tag in rows:
            form = stored_normalized_name(tag)
            if form in doomed and tag.organization_id is not None:
                held_by_org.setdefault(tag.organization_id, set()).add(form)
    if owner_ids:
        rows = db.query(Tag).filter(
            Tag.organization_id.is_(None),
            Tag.user_id.in_(owner_ids),
            _tenant_vocabulary_filter(doomed),
        )
        for tag in rows:
            form = stored_normalized_name(tag)
            if form in doomed and tag.user_id is not None:
                held_by_owner.setdefault(tag.user_id, set()).add(form)
    shadowed: dict[int, set[str]] = {}
    for source in sources:
        if source.organization_id is not None:
            held = held_by_org.get(source.organization_id, set())
        else:
            held = held_by_owner.get(source.user_id, set())
        if held:
            shadowed[source.id] = held
    return shadowed


def rewrite_stored_tag_names(
    db: Session,
    doomed_normalized: set[str],
    replacement: str | None,
    *,
    tenant: tuple[Any, ...],
) -> int:
    """Rewrite the watch-source name lists of one tenant.

    Every list of the tenant is walked: an entry whose normalized form is in
    ``doomed_normalized`` becomes ``replacement`` (or is dropped when it is
    None), and an entry repeating a normalized form already seen in the same
    list is dropped, which can also collapse duplicates unrelated to the change.

    Args:
        db: Session. Flushed when anything changed, never committed.
        doomed_normalized: Normalized names being merged away, renamed or deleted.
        replacement: The new display name, or None to strip the entries.
        tenant: ``tag_scope.tenant_key`` of the tag that changed. A system tag
            reaches every source, except where the source's tenant has its own
            tag of that name.

    Returns:
        The number of sources whose list changed.
    """
    if not doomed_normalized:
        return 0
    query = db.query(WatchSource).filter(WatchSource.tag_names.is_not(None))
    kind = tenant[0]
    if kind == "org":
        query = query.filter(WatchSource.organization_id == tenant[1])
    elif kind == "user":
        query = query.filter(
            WatchSource.user_id == tenant[1], WatchSource.organization_id.is_(None)
        )
    sources = query.order_by(WatchSource.id).all()
    shadowed = _shadowed_names(db, sources, doomed_normalized) if kind == "system" else {}

    changed = 0
    for source in sources:
        names = source.tag_names
        if not isinstance(names, list):
            continue
        active = doomed_normalized - shadowed.get(source.id, set())
        if not active:
            continue
        rewritten = _rewrite_list(names, active, replacement)
        if rewritten != names:
            source.tag_names = rewritten
            changed += 1
    if changed:
        db.flush()
    return changed
