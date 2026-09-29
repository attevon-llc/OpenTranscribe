"""Shared tag resolution — the single path from a supplied name to a ``Tag`` row.

Every creation path (manual tag API, upload prepare, URL ingest, watch sources,
auto-labeling) resolves through :func:`resolve_or_create_tag` so that one
normalized name can never end up stored as two rows *within one vocabulary*.
Resolution is **normalized-exact only**: names differing solely by case,
hyphens, underscores, or repeated whitespace collapse onto the same tag;
anything else is a new tag.

**Scope.** A tag belongs to a **tenant** (``v420_add_tag_organization_id``,
issue #1050): an organization's tags are shared by every member of it, a
personal tag belongs to its owner's personal workspace, and a *system* tag
(``user_id IS NULL AND organization_id IS NULL``, the seeded vocabulary) is in
every tenant. Names are unique only per tenant, so every lookup by name carries
:func:`owned_or_system` for the request's tenant — resolving unscoped would
attach a typed name to another tenant's row, which is both wrong and a
disclosure. Creation is never tenantless: a tag with neither owner nor org is
published to every account, correct only for the bootstrap seed in
``app/initial_data.py``. Background paths resolve in the **file's** tenant
(``MediaFile.organization_id``), never the owner's personal one.

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
on — :func:`stored_normalized_name` and
:func:`accessible_file_ids_subquery` — so ``tag_operations`` and
``tag_collisions`` cannot drift into two answers for one question.
"""

import difflib
import logging
import re
from collections.abc import Iterable
from typing import Literal

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.constants import FUZZY_MATCH_THRESHOLD
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


#: The caller's relationship to a tag — the single vocabulary the whole plane
#: uses. These are **both** the values ``Tag.ownership`` reports on the wire and
#: the values ``GET /tags?scope=`` accepts, deliberately: a filter whose terms
#: differ from the field it filters on is the kind of mismatch nobody notices
#: until the two disagree. The invariant is directly testable — every row a
#: scoped request returns must carry that same ownership.
#:
#: Not a boolean. ``is_shared`` already means "shared *with* me, not mine" on
#: ``CollectionWithCount``; reusing it here for "in the shared vocabulary" gave
#: one name two opposite meanings in one schema module.
#: Typed so the value flows into the schemas' ``Literal`` field without a cast:
#: ``TagOnSelection.ownership`` and ``GET /tags?scope=`` both declare these three
#: strings, and a bare ``str`` return made every call site an mypy ``arg-type``
#: error.
TagOwnership = Literal["mine", "system", "shared_with_me"]

OWNERSHIP_MINE: TagOwnership = "mine"
OWNERSHIP_SYSTEM: TagOwnership = "system"
OWNERSHIP_SHARED_WITH_ME: TagOwnership = "shared_with_me"
TAG_OWNERSHIPS = (OWNERSHIP_MINE, OWNERSHIP_SYSTEM, OWNERSHIP_SHARED_WITH_ME)


def is_system_tag(tag: Tag) -> bool:
    """Whether ``tag`` is system vocabulary — no owner **and** no tenant.

    ``user_id IS NULL`` alone is not enough since v420: an organization tag
    whose creator's account is gone keeps its org and loses its ``user_id``.
    """
    return tag.user_id is None and tag.organization_id is None


def system_tag() -> ColumnElement[bool]:
    """SQL form of :func:`is_system_tag`."""
    return and_(Tag.user_id.is_(None), Tag.organization_id.is_(None))


def in_tenant(user_id: int, organization_id: int | None) -> ColumnElement[bool]:
    """Tags belonging to the request's tenant, system vocabulary excluded.

    Organization context: every tag of that org, whoever created it — that is
    the point of org tags. Personal scope: the caller's own tags that belong to
    no organization.
    """
    if organization_id is not None:
        return Tag.organization_id == organization_id
    return and_(Tag.user_id == user_id, Tag.organization_id.is_(None))


def tag_in_tenant(tag: Tag, user_id: int, organization_id: int | None) -> bool:
    """Python form of :func:`in_tenant`, for a row already loaded."""
    if organization_id is not None:
        return tag.organization_id == organization_id
    return tag.organization_id is None and tag.user_id == user_id


def tenant_first():
    """ORDER BY terms putting a tenant row ahead of a same-named system row.

    ``organization_id`` then ``user_id``, both ASC NULLS LAST (Postgres's
    default for ASC): an org row beats the system row in org context, and an
    owned row beats it in personal scope.
    """
    return (Tag.organization_id, Tag.user_id)


def tag_ownership(tag: Tag, user_id: int) -> TagOwnership:
    """Classify a tag by the caller's relationship to it.

    The single definition, so the list, the collision clusters, and the
    file-detail payload cannot drift into three answers. Maps 1:1 onto the
    three visibility arms and onto what the caller may do:

    ===================  ==========================  ==================
    ``ownership``        Rule                        May mutate?
    ===================  ==========================  ======================
    ``mine``             ``Tag.user_id == user_id``  yes
    ``system``           no owner and no tenant      admin only
    ``shared_with_me``   anything else visible       no (404) — except an
                                                     org admin on an org tag
    ===================  ==========================  ======================

    ``shared_with_me`` covers a tag on a file someone shared with the caller, a
    tag granted through ``tag_share``, and — since v420 — a colleague's tag in
    the caller's organization. All three are visible and appliable; none is the
    caller's to rename, so the UI does not offer a rename that can only 404.

    Args:
        tag: The tag row.
        user_id: The acting user.

    Returns:
        One of :data:`TAG_OWNERSHIPS`.
    """
    if is_system_tag(tag):
        return OWNERSHIP_SYSTEM
    if tag.user_id == user_id:
        return OWNERSHIP_MINE
    return OWNERSHIP_SHARED_WITH_ME


def owned_or_system(user_id: int, organization_id: int | None) -> ColumnElement[bool]:
    """Predicate for the tags a request may **resolve a name against**.

    The request's tenant (:func:`in_tenant`) plus the system vocabulary. This is
    the narrow scope; ``GET /tags`` widens it to shared tags and to tags on an
    accessible file, which is a read-only right (:func:`visible_to`). Which of
    these rows a caller may *rename* is narrower still — see
    ``endpoints/tags/_common._writable_tag_ids``.

    ``organization_id`` is required, not defaulted: a caller that forgot it
    would silently resolve an org member's typed name into their personal
    vocabulary — the cross-tenant bug v420 exists to close.
    """
    return or_(in_tenant(user_id, organization_id), system_tag())


def shared_with(user_id: int):
    """Subquery of tag ids explicitly shared with ``user_id``, direct or via a group.

    The same two arms ``PermissionService`` applies to collections: a grant
    naming the user, or naming a group they belong to. Kept here so the read
    predicate and the "who is this shared with" listing cannot drift apart.
    """
    from sqlalchemy import select

    from app.models.group import UserGroupMember
    from app.models.sharing import TagShare

    member_groups = select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)
    return select(TagShare.tag_id).where(
        or_(
            TagShare.target_user_id == user_id,
            TagShare.target_group_id.in_(member_groups),
        )
    )


def _tenant_bound(organization_id: int | None) -> ColumnElement[bool]:
    """The tenant a *widened* read arm may still reach: never another tenant's row."""
    if organization_id is not None:
        return Tag.organization_id == organization_id
    return Tag.organization_id.is_(None)


def visible_to(db: Session, user_id: int, organization_id: int | None) -> ColumnElement[bool]:
    """Predicate for the tags ``user_id`` is allowed to **read** in a tenant.

    Every arm is bounded to the request's tenant (plus system rows), so a tag
    never surfaces in another tenant — not through a share, and not through a
    file that legacy data left carrying another tenant's row.

    Wider than :func:`owned_or_system` by two arms:

    * a tag **explicitly shared** with the caller, directly or through a group
      (``v386_add_tag_share``). The point of sharing a tag is that the recipient
      uses that same word instead of coining a duplicate.
    * a tag attached to a file the caller can access. Tagging a shared file has
      to put that word in the recipient's picker, or they cannot filter by what
      they are looking at.

    Reading and rewriting are different rights — mutation stays on the narrow
    scope (``endpoints/tags.py:_writable_tag_ids``), since renaming a tag you can
    merely see rewrites its owner's vocabulary everywhere they use it. A share
    grants vocabulary, not administration.

    ``get_accessible_file_ids_subquery`` already covers files shared directly and
    via groups and applies the org tenant gate, so the file arm needs no second
    rule.

    ``get_accessible_file_ids_subquery`` does NOT know about quarantine — it
    covers ownership/sharing only — so the file arm excludes a quarantined
    file's own id explicitly (A2's leak class): without this, a same-named
    tag owned by someone ELSE and attached only to a file shared with the
    caller would keep surfacing in the caller's tag list after that file was
    taken down, even though the file itself now 404s for them.
    """
    from sqlalchemy import select

    quarantined_files = select(MediaFile.id).where(MediaFile.is_quarantined.is_(True))
    attached_to_accessible = select(FileTag.tag_id).where(
        FileTag.media_file_id.in_(
            select(accessible_file_ids_subquery(db, user_id, organization_id))
        ),
        FileTag.media_file_id.not_in(quarantined_files),
    )
    return or_(
        owned_or_system(user_id, organization_id),
        and_(
            _tenant_bound(organization_id),
            or_(Tag.id.in_(shared_with(user_id)), Tag.id.in_(attached_to_accessible)),
        ),
    )


def lookup_existing_tag(
    db: Session, normalized: str, name: str, user_id: int, organization_id: int | None
) -> Tag | None:
    """Find a tag of the request's tenant by normalized name, else by exact name.

    The fallback covers rows written before this service owned creation — the
    bootstrap seed tags ("Important", "Meeting", …) were inserted with a NULL
    ``normalized_name``, which left them invisible to normalized-exact
    resolution yet still able to collide on the unique ``name`` constraint. A
    row found that way is repaired in place (within the caller's transaction) so
    the next lookup takes the indexed fast path.

    Both arms order by :func:`tenant_first`, so the tenant's own row always wins
    over a same-named system row; only when the tenant has none does applying a
    seeded default attach the shared row instead of forking a duplicate.
    """
    scope = owned_or_system(user_id, organization_id)
    tag: Tag | None = (
        db.query(Tag)
        .filter(Tag.normalized_name == normalized, scope)
        .order_by(*tenant_first())
        .first()
    )
    if tag is not None:
        return tag

    tag = db.query(Tag).filter(Tag.name == name, scope).order_by(*tenant_first()).first()
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
    organization_id: int | None,
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
        user_id: The acting user.
        organization_id: The tenant to scan (``None`` = personal). Only that
            tenant's vocabulary plus the system one — suggesting another
            tenant's tag would disclose its name, and applying it (the
            auto-labeler does apply automatically) would attach it across the
            tenant boundary.
        threshold: Minimum similarity ratio.
        candidates: Optional pre-fetched tag list (e.g. an instance-level cache).
            Callers passing a cache are responsible for having scoped it to the
            same tenant — ``AutoLabelService`` keys its cache by
            ``(user_id, organization_id)`` for this reason. When omitted, the
            scoped set is queried.

    Returns:
        The first similar tag, or None when nothing is close enough.
    """
    if not normalize_tag_name(name):
        return None

    pool = (
        candidates
        if candidates is not None
        else db.query(Tag).filter(owned_or_system(user_id, organization_id)).all()
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
    organization_id: int | None,
    source: str = TAG_SOURCE_MANUAL,
    file_id: int | None = None,
) -> Tag:
    """Resolve a supplied name to a tag of the given tenant, or create one there.

    The single path from a supplied name to a ``Tag`` row. Resolution order:

    1. A tag already on ``file_id``, when attaching to a specific file — keeps a
       shared file from carrying the same word twice (:func:`lookup_tag_on_file`).
    2. The tenant's tag, then a same-named **system** tag, so applying a
       seeded default attaches the shared row rather than forking a duplicate
       (:func:`lookup_existing_tag`). In an organization the tenant's tag may be
       a colleague's — reusing it is what makes org vocabulary shared.
    3. Otherwise a new tag in ``organization_id`` (personal when ``None``),
       attributed to ``user_id``.

    Matching is normalized-exact (see :func:`normalize_tag_name`) — a near match
    is *not* resolved here, it becomes a new tag. The insert runs inside a
    SAVEPOINT so that losing a race on ``uq_tag_user_name`` rolls back only the
    failed insert; the caller's other pending writes survive.

    Args:
        db: Database session. Not committed — the caller owns the transaction.
        name: Supplied tag name. Trimmed and clamped to
            :data:`MAX_TAG_NAME_LENGTH`.
        user_id: Owner (personal) or creator (organization) of a tag this
            creates. **Required** — a tag with neither owner nor tenant is a
            system tag, published to every account, which is only ever correct
            for the bootstrap seed.
        organization_id: The tenant to resolve in. For a request, the request's
            tenant; for a background path, the file's ``organization_id``.
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

    existing = lookup_existing_tag(db, normalized, cleaned, user_id, organization_id)
    if existing is not None:
        return existing

    nested = db.begin_nested()
    try:
        tag = Tag(
            name=cleaned,
            user_id=user_id,
            organization_id=organization_id,
            source=source,
            normalized_name=normalized,
        )
        db.add(tag)
        db.flush()
        return tag
    except IntegrityError:
        # Another writer won the race. Roll back only the SAVEPOINT — never the
        # session — so the caller's pending work is untouched, then take theirs.
        nested.rollback()
        winner = lookup_existing_tag(db, normalized, cleaned, user_id, organization_id)
        if winner is not None:
            logger.debug("Lost tag-insert race for %r, using the winning row", cleaned)
            return winner
        raise


def resolve_or_create_tags(
    db: Session,
    names: Iterable[str],
    *,
    user_id: int,
    organization_id: int | None,
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
        user_id: Owner / creator for any tag this creates.
        organization_id: The tenant to resolve in (the file's, on the import
            paths).
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

    scope = owned_or_system(user_id, organization_id)
    found: dict[str, Tag] = {}

    # tenant_first() is ASC NULLS LAST, so a tenant row beats the system row.
    for row in (
        db.query(Tag).filter(Tag.normalized_name.in_(list(wanted)), scope).order_by(*tenant_first())
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
            db.query(Tag)
            .filter(Tag.name.in_(list(missing.values())), scope)
            .order_by(*tenant_first())
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
                tag = Tag(
                    name=cleaned,
                    user_id=user_id,
                    organization_id=organization_id,
                    source=source,
                    normalized_name=normalized,
                )
                db.add(tag)
                db.flush()
            except IntegrityError:
                nested.rollback()
                tag = lookup_existing_tag(db, normalized, cleaned, user_id, organization_id)
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
