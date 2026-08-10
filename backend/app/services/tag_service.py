"""Shared tag resolution — the single path from a supplied name to a ``Tag`` row.

Every creation path (manual tag API, upload prepare, URL ingest, watch sources,
auto-labeling) resolves through :func:`resolve_or_create_tag` so that one
normalized name can never end up stored as two rows *within one vocabulary*.
Resolution is **normalized-exact only**: names differing solely by case,
hyphens, underscores, or repeated whitespace collapse onto the same tag;
anything else is a new tag.

**Scope.** Since ``v374_add_tag_user_id`` a tag is owned (``Tag.user_id``) or
*system* (``user_id IS NULL``, the seeded shared vocabulary), and names are
unique only per owner. Every lookup by name therefore carries
:func:`owned_or_system` — resolving unscoped would attach a typed name to
whichever account's row the planner returned first, which is both wrong and a
disclosure. Creation is never ownerless: an ownerless tag is published to every
account, correct only for the bootstrap seed in ``app/initial_data.py``.

Fuzzy matching lives here too but is deliberately a *separate*, opt-in lookup
(:func:`suggest_similar_tag`). At the 0.85 threshold ``q3-earnings`` and
``q4-earnings`` score 0.909, so resolving fuzzily with no human in the loop
silently attaches the wrong tag — and nothing can split two tags back apart once
combined. Only the auto-labeling path may chain suggest → apply automatically;
on every path where a person supplied the name, a near match is a suggestion to
accept or decline, never an automatic substitution.

Rename, merge, delete, and the impact preview that fronts them live in
:mod:`app.services.tag_operations` — this module stays the *resolution* half
(one supplied name → one row) plus the shared :func:`on_tags_changed` hook that
every tag mutation, here or there, calls.

It is also the single home for the small predicates the whole tag plane agrees
on — :func:`is_awaiting_review`, :func:`stored_normalized_name`, and
:func:`accessible_file_ids_subquery` — so ``tag_operations``, ``tag_review``,
and ``tag_collisions`` cannot drift into three answers for one question.
"""

import difflib
import logging
import re
from collections.abc import Iterable

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.constants import FUZZY_MATCH_THRESHOLD
from app.core.constants import TAG_SOURCE_AUTO_AI
from app.core.constants import TAG_SOURCE_MANUAL
from app.core.exceptions import OpenTranscribeError
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag

logger = logging.getLogger(__name__)

#: Storage width of ``tag.name`` (``VARCHAR(50)``). Supplied names are clamped
#: to this on every path — an over-long name used to reach Postgres unclamped
#: from the tag API and abort the transaction with a ``DataError``.
MAX_TAG_NAME_LENGTH = 50


class InvalidTagNameError(OpenTranscribeError):
    """A supplied tag name is empty once normalized, so it cannot name a tag."""


def normalize_tag_name(name: str) -> str:
    """Normalize a tag name for deduplication comparison.

    Lowercases, replaces hyphens/underscores with spaces, collapses runs of
    whitespace, and trims. This is the single definition of tag-name
    normalization; it is what gets stored in ``Tag.normalized_name``, and it
    matches the SQL backfill in ``v230_add_auto_labeling`` (which also trims
    *after* substitution, so ``"-foo-"`` normalizes to ``"foo"`` and a name made
    only of separators normalizes to the empty string).

    Args:
        name: Raw supplied name.

    Returns:
        The normalized form, or ``""`` for a name with no usable characters.
    """
    if not name:
        return ""
    normalized = re.sub(r"[-_]+", " ", name.lower())
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def clean_tag_name(name: str) -> str:
    """Trim a supplied name and clamp it to the stored column width.

    Args:
        name: Raw supplied name.

    Returns:
        The name as it would be stored: stripped and at most
        :data:`MAX_TAG_NAME_LENGTH` characters.
    """
    if not name:
        return ""
    return name.strip()[:MAX_TAG_NAME_LENGTH].strip()


def stored_normalized_name(tag: Tag) -> str:
    """Return a tag's stored normalized form, recomputing it when legacy-NULL.

    ``Tag.normalized_name`` is maintained by :func:`resolve_or_create_tag` and
    was backfilled by migration v230, but the bootstrap seed tags predate both,
    so the column can still be NULL. Reading it through here means no caller has
    to remember the fallback.

    Args:
        tag: The tag row.

    Returns:
        The stored normalization, or :func:`normalize_tag_name` of the name.
    """
    return tag.normalized_name or normalize_tag_name(tag.name)


def is_awaiting_review(tag: Tag) -> bool:
    """Report whether a tag is still the auto-labeler's to endorse or reject.

    ``Tag.source`` records which path created the row **first**, not who
    endorsed it, so only ``auto_ai`` is awaiting review: ``manual`` and legacy
    NULL (nullable, never backfilled by migration v230) are human origins, and
    ``ai_accepted`` has already left the review set — endorsing it again is a
    no-op the caller should be told about rather than a silent success.

    Args:
        tag: The tag row.

    Returns:
        True when the tag can be accepted or rejected.
    """
    return tag.source == TAG_SOURCE_AUTO_AI


def accessible_file_ids_subquery(db: Session, user_id: int, organization_id: OrgScope = UNSCOPED):
    """Build the caller's accessible-file subquery (the same gate as ``GET /tags``).

    Every tag surface that scopes a count to what the caller can see — the
    impact preview, the usage counts behind the list — goes through this, so a
    confirmation dialog and the list it was opened from cannot disagree about
    which files are in scope.

    Args:
        db: Database session.
        user_id: The acting user.
        organization_id: Tenant scope (``None`` = personal, ``UNSCOPED`` =
            legacy caller, no gate).

    Returns:
        The subquery of accessible file ids, ready to wrap in ``select()``.
    """
    from app.services.permission_service import PermissionService

    return PermissionService.get_accessible_file_ids_subquery(
        db, user_id, organization_id=organization_id
    )


def names_are_similar(a: str, b: str, threshold: float = FUZZY_MATCH_THRESHOLD) -> bool:
    """Report whether two names are similar enough to be considered the same topic.

    Args:
        a: First name.
        b: Second name.
        threshold: Minimum ``SequenceMatcher`` ratio to count as similar.

    Returns:
        True when the normalized forms are equal or score at/above ``threshold``.
    """
    norm_a = normalize_tag_name(a)
    norm_b = normalize_tag_name(b)
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return True
    return difflib.SequenceMatcher(None, norm_a, norm_b).ratio() >= threshold


def owned_or_system(user_id: int) -> ColumnElement[bool]:
    """Predicate for the tags ``user_id`` may resolve against and write.

    A tag is writable-scope when the caller owns it or it is a **system** tag
    (``user_id IS NULL`` — the seeded shared vocabulary). This is the narrow
    scope; ``GET /tags`` widens it to tags attached to an accessible file, which
    is a read-only right (``endpoints/tags.py:_visible_to``).

    Since ``v374_add_tag_user_id`` tag names are unique only **per owner**, so
    every lookup by name must carry this predicate — without it a typed name
    resolves onto whichever account's row the planner happens to return first.
    """
    return or_(Tag.user_id == user_id, Tag.user_id.is_(None))


def visible_to(
    db: Session, user_id: int, organization_id: OrgScope = UNSCOPED
) -> ColumnElement[bool]:
    """Predicate for the tags ``user_id`` is allowed to **read**.

    Wider than :func:`owned_or_system` by one arm: a tag attached to a file the
    caller can access. Tagging a shared file has to put that word in the
    recipient's picker, or they cannot filter by what they are looking at.

    Reading and rewriting are different rights — mutation stays on the narrow
    scope (``endpoints/tags.py:_writable_tag_ids``), since renaming a tag you can
    merely see rewrites its owner's vocabulary everywhere they use it.

    ``get_accessible_file_ids_subquery`` already covers files shared directly and
    via groups and applies the org tenant gate, so sharing needs no second rule.
    """
    from sqlalchemy import select

    attached_to_accessible = select(FileTag.tag_id).where(
        FileTag.media_file_id.in_(
            select(accessible_file_ids_subquery(db, user_id, organization_id))
        )
    )
    return or_(owned_or_system(user_id), Tag.id.in_(attached_to_accessible))


def lookup_existing_tag(db: Session, normalized: str, name: str, user_id: int) -> Tag | None:
    """Find one of the caller's tags by normalized name, else by exact name.

    The fallback covers rows written before this service owned creation — the
    bootstrap seed tags ("Important", "Meeting", …) were inserted with a NULL
    ``normalized_name``, which left them invisible to normalized-exact
    resolution yet still able to collide on the unique ``name`` constraint. A
    row found that way is repaired in place (within the caller's transaction) so
    the next lookup takes the indexed fast path.

    Both arms order by ``Tag.user_id``, which is ASC NULLS LAST in Postgres, so
    the caller's own row always wins over a same-named system row; only when
    they have none does applying a seeded default attach the shared row instead
    of forking a private duplicate.
    """
    scope = owned_or_system(user_id)
    tag: Tag | None = (
        db.query(Tag).filter(Tag.normalized_name == normalized, scope).order_by(Tag.user_id).first()
    )
    if tag is not None:
        return tag

    tag = db.query(Tag).filter(Tag.name == name, scope).order_by(Tag.user_id).first()
    if tag is not None and not tag.normalized_name:
        tag.normalized_name = normalized
        db.flush()
    return tag


def lookup_tag_on_file(db: Session, normalized: str, file_id: int) -> Tag | None:
    """Find a tag already attached to ``file_id`` whose normalized name matches.

    Consulted **before** the caller's own vocabulary when tagging a specific
    file, and the reason a shared file cannot accumulate two rows both named
    "interview". Tag names are unique per owner, so without this the second
    person to tag a shared file forks their own row and the file carries the
    same word twice — visibly duplicated on the detail page, and the reason the
    gallery's ALL-filter had to count ``DISTINCT Tag.name`` rather than
    ``Tag.id``.

    Reusing the row grants nothing: the association is what changes, the tag row
    keeps its owner, and the caller could already see it (``_visible_to`` admits
    every tag on a file they can access).

    Args:
        db: Database session.
        normalized: The normalized form being resolved.
        file_id: The file the tag is about to be attached to.

    Returns:
        The matching attached tag, preferring a system row, else None.
    """
    return (
        db.query(Tag)
        .join(FileTag, FileTag.tag_id == Tag.id)
        .filter(FileTag.media_file_id == file_id, Tag.normalized_name == normalized)
        .order_by(Tag.user_id)
        .first()
    )


def suggest_similar_tag(
    db: Session,
    name: str,
    *,
    user_id: int,
    threshold: float = FUZZY_MATCH_THRESHOLD,
    candidates: list[Tag] | None = None,
) -> Tag | None:
    """Find an existing tag that is a *near* match for a supplied name.

    Opt-in and never called by :func:`resolve_or_create_tag`. Callers on
    human-supplied paths must surface the result as a suggestion to accept or
    decline; only auto-labeling may apply it without confirmation.

    Args:
        db: Database session.
        name: Supplied name to look for.
        user_id: The acting user. Scans only their own vocabulary plus the
            system one — suggesting another account's tag would disclose its
            name, and applying it (the auto-labeler does apply automatically)
            would attach a row the acting user does not own.
        threshold: Minimum similarity ratio.
        candidates: Optional pre-fetched tag list (e.g. an instance-level cache).
            Callers passing a cache are responsible for having scoped it to
            ``user_id`` — ``AutoLabelService`` keys its cache by user for this
            reason. When omitted, the scoped set is queried.

    Returns:
        The first similar tag, or None when nothing is close enough.
    """
    if not normalize_tag_name(name):
        return None

    pool = (
        candidates
        if candidates is not None
        else db.query(Tag).filter(owned_or_system(user_id)).all()
    )
    for existing in pool:
        if names_are_similar(name, existing.name, threshold):
            return existing
    return None


def resolve_or_create_tag(
    db: Session,
    name: str,
    *,
    user_id: int,
    source: str = TAG_SOURCE_MANUAL,
    file_id: int | None = None,
) -> Tag:
    """Resolve a supplied name to an existing tag, or create one owned by ``user_id``.

    The single path from a supplied name to a ``Tag`` row. Resolution order:

    1. A tag already on ``file_id``, when attaching to a specific file — keeps a
       shared file from carrying the same word twice (:func:`lookup_tag_on_file`).
    2. The caller's own tag, then a same-named **system** tag, so applying a
       seeded default attaches the shared row rather than forking a private
       duplicate (:func:`lookup_existing_tag`).
    3. Otherwise a new tag, **owned by** ``user_id``.

    Matching is normalized-exact (see :func:`normalize_tag_name`) — a near match
    is *not* resolved here, it becomes a new tag. The insert runs inside a
    SAVEPOINT so that losing a race on ``uq_tag_user_name`` rolls back only the
    failed insert; the caller's other pending writes survive.

    Args:
        db: Database session. Not committed — the caller owns the transaction.
        name: Supplied tag name. Trimmed and clamped to
            :data:`MAX_TAG_NAME_LENGTH`.
        user_id: Owner for a tag this creates. **Required** — an ownerless tag
            is a system tag, i.e. published to every account, which is only ever
            correct for the bootstrap seed.
        source: Provenance recorded on a newly created tag.
        file_id: The file being tagged, when there is one. Enables step 1.

    Returns:
        The existing or newly created tag (flushed, so ``tag.id`` is populated).

    Raises:
        InvalidTagNameError: The name is empty once normalized.
        IntegrityError: A collision occurred and the winning row still could not
            be found afterwards.
    """
    cleaned = clean_tag_name(name)
    normalized = normalize_tag_name(cleaned)
    if not normalized:
        raise InvalidTagNameError(f"Tag name is empty after normalization: {name!r}")

    if file_id is not None:
        on_file = lookup_tag_on_file(db, normalized, file_id)
        if on_file is not None:
            return on_file

    existing = lookup_existing_tag(db, normalized, cleaned, user_id)
    if existing is not None:
        return existing

    nested = db.begin_nested()
    try:
        tag = Tag(name=cleaned, user_id=user_id, source=source, normalized_name=normalized)
        db.add(tag)
        db.flush()
        return tag
    except IntegrityError:
        # Another writer won the race. Roll back only the SAVEPOINT — never the
        # session — so the caller's pending work is untouched, then take theirs.
        nested.rollback()
        winner = lookup_existing_tag(db, normalized, cleaned, user_id)
        if winner is not None:
            logger.debug("Lost tag-insert race for %r, using the winning row", cleaned)
            return winner
        raise


def resolve_or_create_tags(
    db: Session,
    names: Iterable[str],
    *,
    user_id: int,
    source: str = TAG_SOURCE_MANUAL,
) -> list[Tag]:
    """Resolve a whole list of names in a constant number of queries.

    The batched sibling of :func:`resolve_or_create_tag`, for the bulk paths
    (upload prepare, URL ingest, watch-source poll) where the per-name resolver
    cost 2N round trips — the regression issue #284 A2.8 removed, pinned by
    ``tests/api/test_upload_prep_batching.py``. Same semantics as the single
    resolver: normalized-exact, own row before system row, never fuzzy.

    Two SELECTs regardless of list length — one on ``normalized_name``, one
    covering the legacy rows where that column is still NULL (repaired in place
    as they are found). Only a name that genuinely does not exist costs an
    INSERT, and each keeps its own SAVEPOINT so one lost race cannot poison the
    caller's transaction.

    Args:
        db: Database session. Not committed — the caller owns the transaction.
        names: Supplied names. Blank/unusable ones are dropped, and names that
            normalize to the same form collapse to one tag.
        user_id: Owner for any tag this creates.
        source: Provenance recorded on newly created tags.

    Returns:
        The resolved tags, in first-seen order of their normalized form.
    """
    wanted: dict[str, str] = {}  # normalized -> cleaned, first spelling wins
    for raw in names:
        cleaned = clean_tag_name(raw or "")
        normalized = normalize_tag_name(cleaned)
        if normalized:
            wanted.setdefault(normalized, cleaned)
    if not wanted:
        return []

    scope = owned_or_system(user_id)
    found: dict[str, Tag] = {}

    # ORDER BY user_id is ASC NULLS LAST, so an owned row beats the system row.
    for row in (
        db.query(Tag).filter(Tag.normalized_name.in_(list(wanted)), scope).order_by(Tag.user_id)
    ):
        found.setdefault(str(row.normalized_name), row)

    missing = {norm: name for norm, name in wanted.items() if norm not in found}
    if missing:
        # Fall back to an exact name match, exactly as `lookup_existing_tag`
        # does. This is not only about rows predating the column: a row whose
        # stored normalization is simply *wrong* is invisible to the query above
        # yet still collides on `uq_tag_user_name`, so skipping this would make
        # every such name cost a failed INSERT plus its recovery lookup —
        # 2N queries, which is the regression #284 A2.8 removed.
        for row in (
            db.query(Tag).filter(Tag.name.in_(list(missing.values())), scope).order_by(Tag.user_id)
        ):
            normalized = normalize_tag_name(str(row.name))
            if normalized in missing and normalized not in found:
                if not row.normalized_name:
                    row.normalized_name = normalized
                found[normalized] = row

    resolved: list[Tag] = []
    for normalized, cleaned in wanted.items():
        tag = found.get(normalized)
        if tag is None:
            nested = db.begin_nested()
            try:
                tag = Tag(name=cleaned, user_id=user_id, source=source, normalized_name=normalized)
                db.add(tag)
                db.flush()
            except IntegrityError:
                nested.rollback()
                tag = lookup_existing_tag(db, normalized, cleaned, user_id)
                if tag is None:
                    logger.warning("Could not resolve tag %r after losing its insert race", cleaned)
                    continue
        resolved.append(tag)
    return resolved


def _owner_ids(db: Session, file_ids: list[int]) -> set[int]:
    """Resolve the owning user ids for a set of files in one query."""
    if not file_ids:
        return set()
    rows = db.query(MediaFile.user_id).filter(MediaFile.id.in_(file_ids)).distinct().all()
    return {int(owner) for (owner,) in rows if owner is not None}


def on_tags_changed(
    db: Session,
    file_ids: Iterable[int] | None = None,
    *,
    user_id: int | None = None,
    system_scope: bool = False,
) -> list[int]:
    """Bust tag caches and refresh the search index after any tag mutation.

    The single hook every tag-mutation path calls — the tag API (create,
    attach, detach), the upload helper, and the auto-labeler. Two things happen
    that no caller should have to remember:

    1. The affected users' cached tag lists are dropped — the actor's, plus
       every owner of a touched file, since on a **shared** file the actor and
       the owner are different accounts and both listings changed. Pass
       ``system_scope=True`` when the mutation touched a system tag: that row
       appears in *every* account's list, so nothing narrower is correct.
    2. One partial-update task, carrying the whole affected-file list, refreshes
       those files' search documents, so filtering by tag and searching by tag
       can't drift apart. Same shape as the access-index updater; deliberately
       not the per-user reindex coordinator, which self-skips under a lock on
       exactly the large multi-file merges this exists for.

    Best-effort throughout: neither Redis nor the broker being down may fail the
    mutation that already committed.

    Call this **after** the mutation commits where the caller controls the
    transaction — the refresh task reads the tag rows back from its own session,
    so an uncommitted change is invisible to it. The flush-only callers
    (upload prepare, auto-labeling) commit moments later in the same request or
    task; the task is idempotent and rewrites the whole array, so a re-run
    always converges.

    Args:
        db: Database session, used only to resolve file owners.
        file_ids: Files whose tag set changed. Duplicates are collapsed, so a
            mutation touching one file many times still enqueues one refresh.
            Empty for tag-only changes (creating a tag attaches it to nothing).
        user_id: The acting user, when known — their file listings are busted
            even if they own none of ``file_ids``.
        system_scope: The mutation touched a system tag (``user_id IS NULL``),
            which is in every account's list, so every account's key must go.
            A blunt instrument on a shared Redis — reserved for the case that
            genuinely warrants it rather than applied to every tag write.

    Returns:
        The deduplicated file ids a refresh was enqueued for.
    """
    affected: list[int] = []
    seen: set[int] = set()
    for raw in file_ids or ():
        if raw is None:
            continue
        file_id = int(raw)
        if file_id not in seen:
            seen.add(file_id)
            affected.append(file_id)

    try:
        from app.services.redis_cache_service import redis_cache

        if system_scope:
            redis_cache.invalidate_tags_global()
        for owner_id in _owner_ids(db, affected) | ({user_id} if user_id is not None else set()):
            redis_cache.invalidate_tags(owner_id)
            redis_cache.invalidate_user_files(owner_id)
    except Exception as e:
        logger.debug(f"Tag cache invalidation failed (non-critical): {e}")

    if affected:
        try:
            from app.tasks.search_indexing_task import update_file_tags_index

            update_file_tags_index.delay(affected)
        except Exception as e:
            logger.warning(f"Could not enqueue tag reindex for files {affected}: {e}")

    return affected
