"""Collision clustering and the narrowing filters over the tag list.

A third sibling of :mod:`app.services.tag_operations` (rename / merge / delete)
and :mod:`app.services.tag_review` (accept / reject), split off because both of
those are already at the size limit. This module is the **read** half of tag
management: it decides which tags are duplicates of each other, which are
awaiting review, and which nobody is using — everything ``GET /tags`` and
``GET /tags/collisions`` need in order to ship a finished answer.

Three rules hold it together:

* **Clusters group on the stored normalization — exact equality, never fuzzy.**
  The fuzzy matcher is ``difflib.SequenceMatcher`` at
  :data:`~app.core.constants.FUZZY_MATCH_THRESHOLD` (0.85), which is
  **non-transitive**: ``a ≈ b`` and ``b ≈ c`` with ``a ≉ c`` is ordinary, and
  :func:`~app.services.tag_service.suggest_similar_tag` returns the *first* hit
  in unordered scan order. Clusters built on that would reshuffle between two
  page loads of the same data. Fuzzy hits are returned as a ranked *secondary*
  suggestion per cluster and are never folded into the cluster.
* **The stored normalization is repaired first, on purpose and by name.**
  Migration v230 backfilled ``normalized_name`` and the resolver maintains it,
  but the bootstrap seed tags predate both and a hand-written ``UPDATE tag SET
  name`` leaves it stale. Grouping on a column nothing keeps true would miss
  exactly the legacy rows this exists to find, so
  :func:`refresh_stored_normalization` runs first. It is idempotent and callable
  on its own — a read handler that silently wrote to a globally shared table
  would be a trap.
* **Usage is counted once, through the caller's accessible-file gate.**
  ``usage_count`` and the unused filter both come from
  :func:`accessible_usage_counts`, so they cannot disagree. They used to: the
  list scoped its count while ``/tags/unused`` counted globally, and a tag whose
  only files belonged to someone else reported ``0`` in one place while being
  absent from the other.

Errors are allowed to propagate. The old handlers wrapped their queries in
``try/except`` and returned ``[]``, which renders a broken read as an empty tag
page — indistinguishable from an install with no tags.
"""

from __future__ import annotations

import difflib
import logging
import uuid as uuid_pkg
from dataclasses import dataclass
from dataclasses import field

from sqlalchemy import distinct
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import FUZZY_MATCH_THRESHOLD
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import FileTag
from app.models.media import Tag
from app.services.tag_service import OWNERSHIP_MINE
from app.services.tag_service import OWNERSHIP_SHARED_WITH_ME
from app.services.tag_service import OWNERSHIP_SYSTEM
from app.services.tag_service import TAG_OWNERSHIPS
from app.services.tag_service import accessible_file_ids_subquery
from app.services.tag_service import is_awaiting_review
from app.services.tag_service import normalize_tag_name
from app.services.tag_service import owned_or_system
from app.services.tag_service import tag_ownership
from app.services.tag_service import visible_to

logger = logging.getLogger(__name__)

#: Ownership scopes for the tag list. ``all`` is everything the caller can see;
#: the other three are exactly the ``ownership`` values a row can carry, and
#: partition ``all`` between them. Sharing the vocabulary with
#: ``tag_service.TAG_OWNERSHIPS`` is the point: a scoped request returns rows
#: whose ``ownership`` equals the scope asked for, which is a single testable
#: invariant instead of two lists to keep in step by hand.
SCOPE_ALL = "all"
TAG_SCOPES = (SCOPE_ALL, *TAG_OWNERSHIPS)


@dataclass(frozen=True)
class TagListEntry:
    """One row of the tag list, with everything the SPA renders already decided.

    Attributes:
        uuid: Public identifier.
        name: Stored name.
        source: Origin, ``None`` for rows predating auto-labeling.
        usage_count: Files **the caller can see** carrying this tag.
        awaiting_review: True when the tag is still the auto-labeler's to accept
            or reject. Shipped as a flag so the SPA never re-derives it from the
            origin string.
        ownership: The caller's relationship to the tag — ``mine``, ``system``
            or ``shared_with_me``. Computed here because it needs the caller;
            ``user_id`` itself never reaches the wire.
    """

    uuid: uuid_pkg.UUID
    name: str
    source: str | None
    usage_count: int
    awaiting_review: bool
    ownership: str = OWNERSHIP_MINE


@dataclass(frozen=True)
class TagClusterMember:
    """A tag that shares its normalized name with the rest of its cluster."""

    uuid: uuid_pkg.UUID
    name: str
    source: str | None
    usage_count: int
    suggested_survivor: bool
    ownership: str = OWNERSHIP_MINE


@dataclass(frozen=True)
class TagClusterSuggestion:
    """A tag that is merely *similar* to a cluster, offered beside it.

    Never a cluster member: similarity is non-transitive, so treating it as one
    would make the cluster depend on scan order.
    """

    uuid: uuid_pkg.UUID
    name: str
    source: str | None
    usage_count: int
    similarity: float
    ownership: str = OWNERSHIP_MINE


@dataclass(frozen=True)
class TagCollisionCluster:
    """Tags that normalize to one name, plus the merge the backend recommends.

    Attributes:
        normalized_name: The shared normalized form the cluster is grouped on.
        members: Cluster members, highest usage first.
        suggested_survivor_uuid: The member to keep — the highest-usage one.
        suggestions: Ranked near matches from *outside* the cluster.
    """

    normalized_name: str
    members: list[TagClusterMember] = field(default_factory=list)
    suggested_survivor_uuid: uuid_pkg.UUID | None = None
    suggestions: list[TagClusterSuggestion] = field(default_factory=list)


def refresh_stored_normalization(db: Session, *, user_id: int) -> int:
    """Correct the caller's tags whose stored ``normalized_name`` is missing or stale.

    Idempotent by construction: it writes :func:`normalize_tag_name` of the
    current name and only when the column disagrees, so a second run changes
    nothing. Committed here rather than left to the caller — the repair is a
    real, independently useful write, and a read request's session is closed
    without a commit, which would silently discard it.

    Scoped to ``owned_or_system`` like every other read here. The repair
    discloses nothing on its own, but running it deployment-wide from a per-user
    GET would have one user's page take row locks on every other account's tags.

    Args:
        db: Database session. Committed when anything changed.
        user_id: The acting user, whose vocabulary is repaired.

    Returns:
        The number of rows corrected.
    """
    changed = 0
    for tag in db.query(Tag).filter(owned_or_system(user_id)).all():
        expected = normalize_tag_name(tag.name)
        if tag.normalized_name != expected:
            tag.normalized_name = expected
            changed += 1

    if changed:
        db.flush()
        db.commit()
        logger.info("Repaired stored normalization on %d tag(s)", changed)
    return changed


def accessible_usage_counts(
    db: Session, *, user_id: int, organization_id: OrgScope = UNSCOPED
) -> dict[int, int]:
    """Count each tag's files **within the caller's accessible set**.

    The single definition of tag usage for every read surface: the list's
    ``usage_count``, the unused filter, and the cluster members all call this,
    so none of them can contradict another.

    Args:
        db: Database session.
        user_id: The acting user.
        organization_id: Tenant scope, matching ``GET /tags``.

    Returns:
        ``{tag_id: file_count}``, omitting tags with no accessible file.
    """
    accessible_sq = select(accessible_file_ids_subquery(db, user_id, organization_id))
    rows = (
        db.query(FileTag.tag_id, func.count(distinct(FileTag.media_file_id)))
        .filter(FileTag.media_file_id.in_(accessible_sq))
        .group_by(FileTag.tag_id)
        .all()
    )
    return {int(tag_id): int(count) for tag_id, count in rows if tag_id is not None}


def _clustered_tags(db: Session, *, user_id: int) -> tuple[dict[str, list[Tag]], list[Tag]]:
    """Group tags by stored normalized name, keeping only the collisions.

    Repairs the stored normalization first — grouping on a column nothing keeps
    true would miss the legacy rows this pass exists to surface.

    Returns:
        The collision clusters, and the full tag list the grouping was built
        from (in ``Tag.id`` order). The list is handed back rather than
        discarded because the suggestions pass needs exactly the same rows in
        exactly the same order, and re-querying it was a third full-table scan
        per request.
    """
    refresh_stored_normalization(db, user_id=user_id)

    all_tags = db.query(Tag).filter(owned_or_system(user_id)).order_by(Tag.id).all()
    grouped: dict[str, list[Tag]] = {}
    for tag in all_tags:
        normalized = tag.normalized_name or ""
        if not normalized:
            # A name with no usable characters cannot name a duplicate of
            # anything; grouping the empties together would invent a cluster.
            continue
        grouped.setdefault(normalized, []).append(tag)
    return (
        {normalized: tags for normalized, tags in grouped.items() if len(tags) > 1},
        all_tags,
    )


def _member_sort_key(tag: Tag, usage: dict[int, int]) -> tuple[int, str, int]:
    """Order cluster members: most used first, then by name, then by id.

    Fully determined, so the preselected survivor cannot change between two
    requests over unchanged data.
    """
    return (-usage.get(tag.id, 0), tag.name, tag.id)


def _suggestions_for(
    normalized: str,
    member_ids: set[int],
    candidates: list[Tag],
    usage: dict[int, int],
    *,
    user_id: int,
    threshold: float,
) -> list[TagClusterSuggestion]:
    """Rank the near matches for one cluster, most similar first.

    Ties break on usage and then on name so the order survives a page reload —
    ``SequenceMatcher`` ratios collide often between names of the same shape.
    """
    scored: list[tuple[float, Tag]] = []
    for candidate in candidates:
        if candidate.id in member_ids:
            continue
        candidate_normalized = candidate.normalized_name or ""
        if not candidate_normalized or candidate_normalized == normalized:
            continue
        ratio = difflib.SequenceMatcher(None, normalized, candidate_normalized).ratio()
        if ratio >= threshold:
            scored.append((ratio, candidate))

    scored.sort(key=lambda pair: (-pair[0], -usage.get(pair[1].id, 0), pair[1].name, pair[1].id))
    return [
        TagClusterSuggestion(
            uuid=tag.uuid,
            name=tag.name,
            source=tag.source,
            usage_count=usage.get(tag.id, 0),
            similarity=round(ratio, 4),
            ownership=tag_ownership(tag, user_id),
        )
        for ratio, tag in scored
    ]


def find_tag_collisions(
    db: Session,
    *,
    user_id: int,
    organization_id: OrgScope = UNSCOPED,
    threshold: float = FUZZY_MATCH_THRESHOLD,
) -> list[TagCollisionCluster]:
    """Group duplicate tags into clusters and recommend a survivor for each.

    Grouping is **exact equality on the stored normalization**, so the same data
    yields the same clusters on every request. Near matches are attached as
    ranked suggestions instead of being merged in — see the module docstring for
    why fuzzy clusters are unstable.

    Args:
        db: Database session. Committed if the normalization repair changed rows.
        user_id: The acting user, used to scope the member usage counts.
        organization_id: Tenant scope for those counts.
        threshold: Minimum similarity for a near-match suggestion.

    Returns:
        Clusters ordered by normalized name, each with its members ordered by
        usage and its highest-usage member preselected as the survivor.
    """
    clusters, all_tags = _clustered_tags(db, user_id=user_id)
    if not clusters:
        return []

    usage = accessible_usage_counts(db, user_id=user_id, organization_id=organization_id)

    built: list[TagCollisionCluster] = []
    for normalized in sorted(clusters):
        members = sorted(clusters[normalized], key=lambda tag: _member_sort_key(tag, usage))
        survivor = members[0]
        built.append(
            TagCollisionCluster(
                normalized_name=normalized,
                members=[
                    TagClusterMember(
                        uuid=tag.uuid,
                        name=tag.name,
                        source=tag.source,
                        usage_count=usage.get(tag.id, 0),
                        suggested_survivor=tag.id == survivor.id,
                        ownership=tag_ownership(tag, user_id),
                    )
                    for tag in members
                ],
                suggested_survivor_uuid=survivor.uuid,
                suggestions=_suggestions_for(
                    normalized,
                    {tag.id for tag in members},
                    all_tags,
                    usage,
                    user_id=user_id,
                    threshold=threshold,
                ),
            )
        )
    return built


def colliding_tag_ids(db: Session, *, user_id: int) -> set[int]:
    """Return the ids of every tag that shares its normalized name with another.

    The same pass :func:`find_tag_collisions` uses, so the ``colliding`` list
    filter and the cluster view can never disagree about who is a duplicate.
    """
    clusters, _all_tags = _clustered_tags(db, user_id=user_id)
    return {tag.id for tags in clusters.values() for tag in tags}


def list_tags_filtered(
    db: Session,
    *,
    user_id: int,
    organization_id: OrgScope = UNSCOPED,
    awaiting_review: bool = False,
    unused: bool = False,
    colliding: bool = False,
    scope: str = SCOPE_ALL,
) -> list[TagListEntry]:
    """List tags with usage counts, optionally narrowed.

    Filters **combine** (logical AND) rather than replacing one another, so
    "auto-labeled tags nobody uses" is one request.

    Args:
        db: Database session.
        user_id: The acting user; usage is scoped to files they can access.
        organization_id: Tenant scope for that gate.
        awaiting_review: Keep only tags the auto-labeler created and nobody has
            accepted or rejected yet.
        unused: Keep only tags with no accessible file — the exact complement of
            ``usage_count > 0``, from the same count.
        colliding: Keep only tags sharing a normalized name with another tag.
        scope: ``all`` (default) or one of :data:`~app.services.tag_service.
            TAG_OWNERSHIPS` — ``mine``, ``system``, ``shared_with_me``. Ownership
            is a different axis from the other three filters, which is why it is
            a separate parameter rather than a fourth boolean: "my unused tags"
            has to be expressible.

    Note:
        The base scope here is ``visible_to``, not ``owned_or_system``: a tag
        attached to a file shared with the caller belongs in their list, or they
        cannot filter by a tag they can see on screen. The narrower scope is for
        the *write* surfaces and for ``/unused``, where the shared-file arm is
        vacuous by definition.

    Returns:
        Entries ordered by usage (descending) then name, matching the order the
        list has always shipped in.
    """
    usage = accessible_usage_counts(db, user_id=user_id, organization_id=organization_id)
    collision_ids = colliding_tag_ids(db, user_id=user_id) if colliding else set()

    entries: list[TagListEntry] = []
    # Each branch is the SQL form of the matching `tag_ownership` arm, so the
    # filter and the field it filters on cannot disagree.
    query = db.query(Tag).filter(visible_to(db, user_id, organization_id))
    if scope == OWNERSHIP_MINE:
        query = query.filter(Tag.user_id == user_id)
    elif scope == OWNERSHIP_SYSTEM:
        query = query.filter(Tag.user_id.is_(None))
    elif scope == OWNERSHIP_SHARED_WITH_ME:
        query = query.filter(Tag.user_id.is_not(None), Tag.user_id != user_id)

    for tag in query.order_by(Tag.name).all():
        count = usage.get(tag.id, 0)
        if awaiting_review and not is_awaiting_review(tag):
            continue
        if unused and count:
            continue
        if colliding and tag.id not in collision_ids:
            continue
        entries.append(
            TagListEntry(
                uuid=tag.uuid,
                name=tag.name,
                source=tag.source,
                usage_count=count,
                awaiting_review=is_awaiting_review(tag),
                ownership=tag_ownership(tag, user_id),
            )
        )

    entries.sort(key=lambda entry: (-entry.usage_count, entry.name))
    return entries


def list_unused_tag_rows(
    db: Session, *, user_id: int, organization_id: OrgScope = UNSCOPED
) -> list[Tag]:
    """Return the tag rows no accessible file carries.

    Reads the same counts as :func:`list_tags_filtered`, so ``/tags/unused`` and
    a ``usage_count`` of 0 always name the same set of tags.

    Args:
        db: Database session.
        user_id: The acting user.
        organization_id: Tenant scope.

    Returns:
        The unused rows, ordered by name.
    """
    usage = accessible_usage_counts(db, user_id=user_id, organization_id=organization_id)
    rows = db.query(Tag).filter(owned_or_system(user_id)).order_by(Tag.name).all()
    return [tag for tag in rows if not usage.get(tag.id, 0)]
