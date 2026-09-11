"""Postgres full-text search over ``media_file.summary_data`` (issue #462).

A summary lives only in ``media_file.summary_data`` (#67 retired the
``transcript_summaries`` OpenSearch index), so it has no search-index presence
at all today. This runs ``websearch_to_tsquery`` against Postgres directly
rather than adding a new OpenSearch document type — the corpus this searches
is small (one JSONB blob per file) and the access-control authority
(:meth:`PermissionService.get_accessible_file_ids_subquery`) is a SQL
predicate already, so a second round trip through OpenSearch would buy
nothing but a second place for that rule to drift.

Matching happens against the RAW (unredacted) ``summary_data`` — the same
precedent ``search/hybrid_search_service.py`` already sets for transcript
chunks, whose module docstring is explicit that "transcript_chunks stores
transcript text UNREDACTED by design" and masking happens only at snippet
time. A user's own masked-content search must still be able to find the
section their policy would mask; only the returned snippets are masked, and
only the leaves that are actually returned — see ``mask_summary_leaf`` below
(issue #822: this used to mask the WHOLE tree per matching file and then
discard everything but the returned leaves, so a detector outage anywhere in
a file withheld results a user was never going to see).

Access control: this reuses ``PermissionService.get_accessible_file_ids_subquery``
verbatim — the single authority the whole codebase already routes owner-scoped
listings through — rather than writing a second sharing predicate here.

Quarantine (abuse/DMCA takedown) is applied here too, via
``takedown_service.exclude_quarantined`` — as a PRE-filter, not a post-filter on the
hit list, because a post-filter cannot correct total/offset and a count that includes
a taken-down file is a content oracle (issue #818, same class as #876's
``/search/count``).

The request's metadata filters (date range, tags, collections, speakers, …) are
applied the same way and for the same reason — see ``summary_filters.py``, which
owns the predicates and why they mirror the transcript leg rather than the
gallery. Results are ordered by ``ts_rank`` (issue #831 item 2); before that they
came back in ``id`` order, i.e. newest-first, with no relevance signal at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy import ARRAY
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import bindparam
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.services.permission_service import PermissionService
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.summary_redaction import _UNMASKED_TOP_LEVEL_KEYS
from app.services.redaction.summary_redaction import mask_summary_leaf
from app.services.search.summary_filters import SummarySearchFilters
from app.services.search.summary_filters import summary_filter_predicates
from app.services.takedown_service import exclude_quarantined

logger = logging.getLogger(__name__)

#: Longest snippet returned for one matching leaf. Summary sections are
#: normally a sentence or two; this only guards against a pathological custom
#: prompt producing a very long leaf.
_MAX_SNIPPET_CHARS = 400


@dataclass
class SummarySectionMatch:
    """One matching leaf inside a summary, identified by its JSON key-path.

    ``key_path`` is a JS/Python-style path (``major_topics[0].key_points[2]``)
    so the frontend can walk ``summary_data`` with it directly to scroll to the
    matching section — no separate id scheme to keep in sync with the summary
    renderer.
    """

    key_path: str
    snippet: str


@dataclass
class SummaryHit:
    """A file-level summary search result."""

    file_uuid: str
    file_id: int
    title: str
    matches: list[SummarySectionMatch] = field(default_factory=list)


@dataclass
class SummarySearchResult:
    """A page of summary search results."""

    results: list[SummaryHit]
    total: int


def _walk_leaves(node: Any, path: str) -> list[tuple[str, str]]:
    """Recursively collect ``(key_path, text)`` for every string leaf.

    Mirrors ``summary_redaction._mask_node``'s walk (dict / list / string
    leaf), but also emits the path alongside the leaf, and skips the same
    ``_UNMASKED_TOP_LEVEL_KEYS`` (machine-generated ``metadata`` — provider,
    model, timings — is not searchable content).
    """
    if isinstance(node, str):
        return [(path, node)] if node.strip() else []
    if isinstance(node, dict):
        out: list[tuple[str, str]] = []
        for key, value in node.items():
            if path == "" and key in _UNMASKED_TOP_LEVEL_KEYS:
                continue
            child_path = f"{path}.{key}" if path else key
            out.extend(_walk_leaves(value, child_path))
        return out
    if isinstance(node, list):
        out = []
        for index, item in enumerate(node):
            out.extend(_walk_leaves(item, f"{path}[{index}]"))
        return out
    return []


def _matching_leaf_indices(
    db: Session, row_idx: list[int], leaf_idx: list[int], texts: list[str], query: str
) -> set[tuple[int, int]]:
    """Return the ``(row_idx, leaf_idx)`` pairs whose leaf text satisfies ``query``.

    ONE query for the WHOLE PAGE (issue #822's remaining cost, after Unit 3
    stopped masking every leaf and this narrowed the leaf-matching lookup
    itself) — not one query per file. ``row_idx`` / ``leaf_idx`` / ``texts`` are
    three parallel arrays: position ``i`` in each names one leaf, and
    ``unnest`` on all three together is what keeps them aligned through
    Postgres rather than relying on a second, implicit ordinality column.
    **Array-parallelism invariant**: callers must build all three with the
    same per-leaf iteration order and never reorder one without the others,
    or a hit gets attributed to the wrong file's leaf entirely.

    Uses the same ``simple``-config ``websearch_to_tsquery`` semantics as the
    document-level predicate, instead of a second, looser matching rule (a
    plain substring check) that could disagree with what actually matched —
    this must not drift from that document-level predicate.
    """
    if not texts:
        return set()
    stmt = text(
        """
            SELECT u.row_idx, u.leaf_idx
            FROM unnest(CAST(:row_idx AS int[]), CAST(:leaf_idx AS int[]), CAST(:texts AS text[]))
                 AS u(row_idx, leaf_idx, leaf_text)
            WHERE to_tsvector('simple', leaf_text) @@ websearch_to_tsquery('simple', :q)
            """
    ).bindparams(
        bindparam("row_idx", type_=ARRAY(Integer)),
        bindparam("leaf_idx", type_=ARRAY(Integer)),
        bindparam("texts", type_=ARRAY(String)),
        bindparam("q", type_=String),
    )
    rows = db.execute(
        stmt, {"row_idx": row_idx, "leaf_idx": leaf_idx, "texts": texts, "q": query}
    ).fetchall()
    return {(int(row[0]), int(row[1])) for row in rows}


def _snippet(text_value: str) -> str:
    if len(text_value) <= _MAX_SNIPPET_CHARS:
        return text_value
    return text_value[:_MAX_SNIPPET_CHARS].rstrip() + "…"


def search_summaries(
    db: Session,
    query: str,
    user_id: int,
    *,
    organization_id: OrgScope = UNSCOPED,
    page: int = 1,
    page_size: int = 20,
    redaction_cfg: EffectiveRedactionConfig | None = None,
    include_quarantined: bool = False,
    filters: SummarySearchFilters | None = None,
) -> SummarySearchResult:
    """Full-text search over accessible files' AI summaries.

    Args:
        db: Session.
        query: ``websearch_to_tsquery`` search text (supports quoted phrases,
            ``OR``, and leading ``-`` for negation, same as the web-search
            operators Postgres already exposes).
        user_id: The requesting user.
        organization_id: Tenant scope. Pass the real ``ctx.org_id`` — this is
            the single access-control authority for this search, not a second
            copy of the sharing rule.
        page: 1-indexed page number.
        page_size: Results per page (files, not leaves).
        redaction_cfg: The requesting user's effective redaction config. Each
            leaf actually RETURNED is masked under it, individually, via
            ``mask_summary_leaf`` — never batched (see
            ``redaction/summary_redaction.py``'s module docstring for why: a
            batched detector pass drops repeated names after their first
            mention). Only the leaves that make it into the response are
            masked — a leaf that matched the document-level predicate but was
            not selected as one of this file's matches is never examined
            (issue #822). ``None`` (the default) means "resolve nothing,
            return unmasked" and must only be passed by a caller that has
            independently decided masking does not apply.
        include_quarantined: Admin review bypass, matching the
            ``exclude_quarantined`` convention used elsewhere (e.g.
            ``files/__init__.py``'s ``include_quarantined=is_admin``). Default
            False: a taken-down file must not appear in, or be COUNTED by, a
            normal user's summary search.
        filters: The request's metadata filters (date range, tags, collections,
            speakers, …). Applied as PRE-filters in the same query as the count
            and the page offset — issue #818's rule, for the same reason: a
            filter applied after paging leaves ``total`` describing a different
            set than the page does. ``None`` means "no metadata filters", which
            is what every non-endpoint caller wants.

    Returns:
        A page of file-level hits ordered by relevance (``ts_rank``,
        descending), each carrying every matching leaf's key-path and (masked)
        snippet text.

    Raises:
        SummaryMaskingUnavailableError: propagated from ``mask_summary_leaf``
            when a detector feeding one of the caller's enabled categories
            could not run on a leaf about to be returned. The caller must
            fail closed (503), not fall back to the unmasked summary. A
            detector outage on a leaf that was never going to be returned no
            longer raises this — see the note on the narrowing above.
    """
    accessible = PermissionService.get_accessible_file_ids_subquery(
        db, user_id, organization_id=organization_id
    )

    ts_document = func.to_tsvector("simple", cast(MediaFile.summary_data, String))
    ts_query = func.websearch_to_tsquery("simple", query)
    predicate = ts_document.op("@@")(ts_query)

    base_filter = [
        MediaFile.id.in_(select(accessible.c[0])),
        MediaFile.summary_data.isnot(None),
        func.jsonb_typeof(MediaFile.summary_data) == "object",
        predicate,
    ]
    # The request's metadata filters join the access-control and quarantine
    # predicates HERE, in the one query that both counts and pages — never as a
    # pass over the returned hits (issue #818, and issue #831's own count
    # consistency requirement).
    base_filter.extend(summary_filter_predicates(filters or SummarySearchFilters()))

    def _scoped(q):
        return exclude_quarantined(q.filter(*base_filter), include_quarantined=include_quarantined)

    total = _scoped(db.query(func.count(MediaFile.id))).scalar() or 0

    # Relevance, with `id` as the tie-break (issue #831 item 2). `ts_rank` over
    # the same `simple`-config document vector the WHERE clause already matches
    # against: more occurrences of the query's terms in a summary ranks it
    # higher, and Postgres computes the vector once per row either way.
    #
    # NOT `ts_rank_cd`: cover density scores how CLOSE the query's terms sit to
    # one another, and this document is a serialized JSONB blob whose leaf order
    # is an artifact of the summary schema (and whose "adjacent" words are often
    # separated by JSON punctuation), so proximity here measures the container,
    # not the prose.
    #
    # The `id` tie-break is not decoration: `ts_rank` ties are the common case on
    # short summaries, and an ORDER BY that does not totally order the rows lets
    # Postgres return them differently per page, which duplicates and drops hits
    # across a paginated result set.
    rank = func.ts_rank(ts_document, ts_query)

    rows = (
        _scoped(
            db.query(
                MediaFile.id,
                MediaFile.uuid,
                MediaFile.title,
                MediaFile.filename,
                MediaFile.summary_data,
            )
        )
        .order_by(rank.desc(), MediaFile.id.desc())
        .offset(max(0, (page - 1) * page_size))
        .limit(page_size)
        .all()
    )

    # Walk every row's leaves BEFORE matching, so leaf-level identification can
    # be issued as ONE query for the whole page instead of one per file. The
    # three arrays below are built in the same per-leaf order for every row —
    # the array-parallelism invariant `_matching_leaf_indices` requires.
    per_row_leaves: list[list[tuple[str, str]]] = [_walk_leaves(row[4], "") for row in rows]
    batch_row_idx: list[int] = []
    batch_leaf_idx: list[int] = []
    batch_texts: list[str] = []
    for r, leaves in enumerate(per_row_leaves):
        for leaf_i, (_path, leaf_text) in enumerate(leaves):
            batch_row_idx.append(r)
            batch_leaf_idx.append(leaf_i)
            batch_texts.append(leaf_text)

    matched_pairs = (
        _matching_leaf_indices(db, batch_row_idx, batch_leaf_idx, batch_texts, query)
        if batch_texts
        else set()
    )
    matched_by_row: dict[int, set[int]] = {}
    for row_i, leaf_i in matched_pairs:
        matched_by_row.setdefault(row_i, set()).add(leaf_i)

    results: list[SummaryHit] = []
    for r, (file_id, file_uuid, title, filename, _summary_data) in enumerate(rows):
        raw_leaves = per_row_leaves[r]
        paths = [p for p, _ in raw_leaves]
        leaf_texts = [t for _, t in raw_leaves]
        matched_positions = matched_by_row.get(r, set())

        # The document-level predicate runs over the whole serialized JSON, so
        # a query whose terms are split across two different leaves (or that
        # only match once every leaf's JSON punctuation is glued together) can
        # match at the document level with no single leaf matching on its own.
        # Keep the file in the page (`total` already counted it) with an empty
        # `matches` list rather than dropping it and desynchronizing the count
        # from what the caller actually gets back.
        #
        # Mask only the leaves actually returned (issue #822) — not the whole
        # tree. `mask_summary_leaf` shares `resolve_summary_leaf_policy` with
        # the whole-tree `mask_summary`, so this is the same detector pass per
        # leaf either way; it narrows WHICH leaves are examined, not how many
        # detector calls one leaf costs. Fail-closed narrows to match: a
        # detector outage on a leaf about to be DISCLOSED still 503s, but an
        # outage on a leaf that matched the document-level predicate and was
        # never going to be shown no longer withholds results the caller was
        # never going to see. A real detector outage is process-wide anyway,
        # so this does not weaken the guarantee — it removes a false positive.
        matches = [
            SummarySectionMatch(
                key_path=paths[i],
                snippet=_snippet(
                    mask_summary_leaf(leaf_texts[i], redaction_cfg)
                    if redaction_cfg is not None
                    else leaf_texts[i]
                ),
            )
            for i in sorted(matched_positions)
        ]
        results.append(
            SummaryHit(
                file_uuid=str(file_uuid),
                file_id=file_id,
                title=title or filename or "",
                matches=matches,
            )
        )

    return SummarySearchResult(results=results, total=total)
