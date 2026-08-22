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
section their policy would mask; only the returned snippet is masked. See
``mask_summary`` below.

Access control: this reuses ``PermissionService.get_accessible_file_ids_subquery``
verbatim — the single authority the whole codebase already routes owner-scoped
listings through — rather than writing a second sharing predicate here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy import ARRAY
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
from app.services.redaction.summary_redaction import mask_summary

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


def _get_by_path(node: Any, path: str) -> Any:
    """Look up the value at a ``key_path`` produced by :func:`_walk_leaves`.

    ``mask_summary`` preserves the container shape exactly (same keys, same
    list order, only string leaves rewritten), so a path collected from the
    RAW tree always resolves on the MASKED tree too.
    """
    current = node
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        current = current[int(part[1:-1])] if part.startswith("[") else current[part]
    return current


def _matching_leaf_indices(db: Session, texts: list[str], query: str) -> set[int]:
    """Return the positions in ``texts`` whose tokens satisfy ``query``.

    One batched query per document (not one per leaf) using
    ``unnest(...) WITH ORDINALITY`` so leaf-level identification uses the same
    ``simple``-config ``websearch_to_tsquery`` semantics as the document-level
    predicate, instead of a second, looser matching rule (a plain substring
    check) that could disagree with what actually matched.
    """
    if not texts:
        return set()
    stmt = text(
        """
            SELECT ord - 1 AS idx
            FROM unnest(CAST(:texts AS text[])) WITH ORDINALITY AS u(leaf_text, ord)
            WHERE to_tsvector('simple', leaf_text) @@ websearch_to_tsquery('simple', :q)
            """
    ).bindparams(
        bindparam("texts", type_=ARRAY(String)),
        bindparam("q", type_=String),
    )
    rows = db.execute(stmt, {"texts": texts, "q": query}).fetchall()
    return {int(row[0]) for row in rows}


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
        redaction_cfg: The requesting user's effective redaction config. Every
            matching summary is masked under it before its snippets are
            returned, per-leaf, via ``mask_summary`` — never batched (see that
            module's docstring for why: a batched detector pass drops repeated
            names after their first mention). ``None`` (the default) means
            "resolve nothing, return unmasked" and must only be passed by a
            caller that has independently decided masking does not apply.

    Returns:
        A page of file-level hits, each carrying every matching leaf's
        key-path and (masked) snippet text.

    Raises:
        SummaryMaskingUnavailableError: propagated from ``mask_summary`` when
            a detector feeding one of the caller's enabled categories could
            not run. The caller must fail closed (503), not fall back to the
            unmasked summary.
    """
    accessible = PermissionService.get_accessible_file_ids_subquery(
        db, user_id, organization_id=organization_id
    )

    ts_document = func.to_tsvector("simple", cast(MediaFile.summary_data, String))
    ts_query = func.websearch_to_tsquery("simple", query)
    predicate = ts_document.op("@@")(ts_query)

    base_filter = (
        MediaFile.id.in_(select(accessible.c[0])),
        MediaFile.summary_data.isnot(None),
        func.jsonb_typeof(MediaFile.summary_data) == "object",
        predicate,
    )

    total = db.query(func.count(MediaFile.id)).filter(*base_filter).scalar() or 0

    rows = (
        db.query(
            MediaFile.id,
            MediaFile.uuid,
            MediaFile.title,
            MediaFile.filename,
            MediaFile.summary_data,
        )
        .filter(*base_filter)
        .order_by(MediaFile.id.desc())
        .offset(max(0, (page - 1) * page_size))
        .limit(page_size)
        .all()
    )

    results: list[SummaryHit] = []
    for file_id, file_uuid, title, filename, summary_data in rows:
        raw_leaves = _walk_leaves(summary_data, "")
        paths = [p for p, _ in raw_leaves]
        leaf_texts = [t for _, t in raw_leaves]
        matched_positions = _matching_leaf_indices(db, leaf_texts, query) if leaf_texts else set()

        masked = (
            mask_summary(summary_data, redaction_cfg) if redaction_cfg is not None else summary_data
        )

        # The document-level predicate runs over the whole serialized JSON, so
        # a query whose terms are split across two different leaves (or that
        # only match once every leaf's JSON punctuation is glued together) can
        # match at the document level with no single leaf matching on its own.
        # Keep the file in the page (`total` already counted it) with an empty
        # `matches` list rather than dropping it and desynchronizing the count
        # from what the caller actually gets back.
        matches = [
            SummarySectionMatch(
                key_path=paths[i], snippet=_snippet(str(_get_by_path(masked, paths[i])))
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
