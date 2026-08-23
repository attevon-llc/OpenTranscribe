"""The **target** index mapping for the digest tier — defined here, applied by Stage 3.

Stage 2 produces artifacts in Postgres and changes nothing about OpenSearch. But the
mapping those artifacts will be indexed under has to be pinned *now*, because #383 Phase 3
gets exactly **one** ``_INDEX_VERSION`` bump and one full reindex; anything discovered
afterwards costs a second one across every deployment. So this module is a specification
with executable parts: the constants, id scheme and document builder Stage 3 imports, and
the tests that hold them to the shape agreed here.

Nothing in this module writes to OpenSearch. It is imported by the tests and, from
Stage 3, by ``services/search/indexing_service.py``.

## What Stage 3 must do with it

1. Add :data:`TARGET_MAPPING_ADDITIONS` to ``TRANSCRIPT_CHUNKS_INDEX_BODY["mappings"]
   ["properties"]`` and set ``_INDEX_VERSION = 6`` (:data:`TARGET_INDEX_VERSION`).
2. Add :data:`DOC_TYPE_FIELD` to the predicate in ``indexing_service.chunk_plane_query``
   — one line, one place (that function exists for this).
3. Put :func:`chunk_plane_clause` into ``_build_filters`` **and** into the four readers
   that build their own filter lists (addendum G3): ``get_suggestions``,
   ``get_available_filters``, and the two reindex-status aggregations in
   ``api/endpoints/search.py``; plus the two "is this file indexed?" counters (G4).
4. Leave the ACL and tenant rewrites alone (G5) — ``update_file_access_index`` keys on
   ``file_id`` and the tenant backfill keys on ``file_uuid``, so digest documents carry
   **both** or a share revocation strands a readable digest. That is a permission leak,
   not a relevance bug.

## The three mechanics that are easy to get wrong

- ``doc_type`` **must be mapped explicitly** as ``keyword`` (G2) — but understand what that
  does and does not buy, because getting the reason wrong leads straight to shipping the
  mapping without the compat arm. Probed against OpenSearch 3.4: the index is not
  ``dynamic: strict``, so an unmapped ``doc_type`` is dynamically mapped ``text`` +
  ``.keyword``, and a bare ``term`` on the field name **does** match all four planned
  values — they are single lowercase tokens the standard analyzer passes through unchanged.
  The explicit mapping is about not depending on that (a value with a hyphen, a case
  change, or an analyzer change would break it silently), not about the ``term`` failing
  today. **The real hazard is the missing field**: every chunk document already indexed
  carries no ``doc_type`` at all, so a bare ``term`` matches *none* of them — the #400
  count gate returns 0 and pruning silently stops working for the whole installed corpus.
  An explicit ``keyword`` mapping does nothing for documents written before it existed.
  Hence :func:`chunk_plane_clause`, and hence it is mandatory rather than defensive.
- Document ids are ``{file_uuid}_{chunk_index}``, so ``{uuid}_0`` is already taken by
  chunk 0. Digests use :func:`digest_document_id` (G2).
- ``index.sort.field`` includes ``chunk_index``, so a digest document needs one.
  :func:`digest_chunk_index` returns a negative sentinel, which also sorts digests ahead
  of the chunks of the same file — convenient, and never colliding with a real index.
"""

from __future__ import annotations

from typing import Any

from . import sizing

#: ``_INDEX_VERSION`` after Stage 3's single bump.
TARGET_INDEX_VERSION = 6

#: The discriminator. ``doc_type``, not ``source_type`` — #403 **D1** settles the conflict
#: between the #383 and #362 plans in favour of this spelling. One field, one value set,
#: one compat helper, imported by every reader.
DOC_TYPE_FIELD = "doc_type"

#: A transcript chunk. Legacy documents predate the field and carry nothing, which is why
#: every read needs :func:`chunk_plane_clause` rather than a bare ``term``.
DOC_TYPE_CHUNK = "chunk"

#: A digest section of a transcript (this stage's output).
DOC_TYPE_DIGEST = "digest"

DOC_TYPES: tuple[str, ...] = (
    DOC_TYPE_CHUNK,
    DOC_TYPE_DIGEST,
)

#: The doc_types that are *someone's own words*, i.e. what the search UI and the chunk
#: plane mean by a result. Digests are derived text and must not surface as if a speaker
#: had said them.
VERBATIM_DOC_TYPES: tuple[str, ...] = (DOC_TYPE_CHUNK,)

#: Mapping entries Stage 3 adds to the chunks index.
#:
#: ``embedding_text`` is the zero-LLM contextualization field the ingest pipeline's
#: ``field_map`` gets repointed at. It is ``text`` and **not** searched by BM25 by any
#: query built today (``content`` stays the BM25 field) — it exists to be embedded.
#: ``index: False`` is deliberately NOT set: the ingest pipeline reads the source, but
#: leaving it searchable-but-unsearched costs little and makes the field debuggable from
#: ``_search`` instead of only from ``_source``.
TARGET_MAPPING_ADDITIONS: dict[str, dict[str, Any]] = {
    DOC_TYPE_FIELD: {"type": "keyword"},
    "embedding_text": {"type": "text", "analyzer": "transcript"},
    "digest_section": {"type": "integer"},
}


def chunk_plane_clause() -> dict[str, Any]:
    """The compatibility-armed "this is a transcript chunk" filter clause.

    Existing indices have no ``doc_type`` at all, and ``_check_index_version`` only *logs*
    when it sees an old version — a deployment can run a v5 index for months. A bare
    ``{"term": {"doc_type": "chunk"}}`` would therefore exclude the entire corpus on every
    such deployment. This is described in the review addendum as the single most likely
    production break in the plan.

    Returns:
        A ``bool``/``should`` clause matching documents that either declare themselves
        chunks or predate the field. Safe to drop into any ``filter`` list.
    """
    return {
        "bool": {
            "should": [
                {"term": {DOC_TYPE_FIELD: DOC_TYPE_CHUNK}},
                {"bool": {"must_not": {"exists": {"field": DOC_TYPE_FIELD}}}},
            ],
            "minimum_should_match": 1,
        }
    }


def digest_plane_clause() -> dict[str, Any]:
    """The inverse: match only digest documents. No compat arm — digests are all new."""
    return {"term": {DOC_TYPE_FIELD: DOC_TYPE_DIGEST}}


def digest_document_id(file_uuid: str, section_index: int) -> str:
    """``{uuid}_digest_{n}`` — never colliding with ``{uuid}_{chunk_index}``.

    The section number is part of the id, not just a field, so re-indexing a file whose
    digest re-sectioned to fewer sections leaves an orphan the same way a shorter re-chunk
    does (#400). Stage 3's delete-before-reindex must cover the digest plane too.
    """
    return f"{file_uuid}_digest_{section_index}"


def digest_chunk_index(section_index: int) -> int:
    """Negative sentinel for ``index.sort.field``: section 0 → -1, section 1 → -2, …

    Zero is a real chunk index, so the sentinel starts at -1. Negative also means "not a
    position in the transcript", which is true, and it sorts a file's digests ahead of its
    chunks.
    """
    return -1 - int(section_index)


def build_embedding_text(
    *,
    title: str | None,
    recorded_at: str | None,
    roster: list[str],
    body: str,
    max_roster: int = 6,
) -> str:
    """Compose the ``embedding_text`` a digest document is embedded from.

    ``"{title} | {date} | participants: {roster}\\n\\n{body}"``. The header is what makes a
    digest retrievable by "the Acme kickoff" or "the meeting with Dana" when neither
    phrase is in the transcript — contextualization for free, no LLM.

    The roster is truncated because the header shares a 128-wordpiece window with the
    digest text (:mod:`.sizing`); an eleven-person meeting would otherwise spend the whole
    budget on names. Caller keeps within :data:`sizing.HEADER_WORDPIECE_RESERVE`; check
    with :func:`embedding_text_fits`.

    Args:
        title: File title.
        recorded_at: ISO date string (the date part is used).
        roster: Speaker display names, already sorted by the facts builder.
        body: The digest section text.
        max_roster: Names before eliding.

    Returns:
        The composed string.
    """
    header_parts: list[str] = []
    if title:
        header_parts.append(str(title).strip())
    if recorded_at:
        header_parts.append(str(recorded_at)[:10])
    if roster:
        shown = roster[:max_roster]
        names = ", ".join(shown)
        if len(roster) > max_roster:
            names += f", +{len(roster) - max_roster} more"
        header_parts.append(f"participants: {names}")
    header = " | ".join(header_parts)
    return f"{header}\n\n{body}" if header else body


def embedding_text_fits(embedding_text: str) -> bool:
    """True when the composed text fits the measured embedding window.

    Uses the whole window rather than subtracting the header reserve a second time — the
    header is already inside *embedding_text* here.
    """
    return sizing.estimate_wordpieces(embedding_text) <= sizing.EMBEDDING_CONTENT_WORDPIECES


def build_digest_documents(
    *,
    file_uuid: str,
    file_id: int,
    digest: dict[str, Any],
    facts: dict[str, Any],
    base_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Turn a stored digest into the documents Stage 3 will index.

    Pure: builds dicts, indexes nothing. Stage 3 supplies *base_metadata* (the per-file
    fields a chunk document already carries — ``user_id``, ``title``, ``tags``,
    ``upload_time``, ``collection_ids``, ``accessible_user_ids``, ``organization_id``,
    ``language``, ``duration``, …) so the ACL and tenant rewrite paths reach digests
    unchanged.

    Args:
        file_uuid: File UUID. Present on the document **and** used for its id.
        file_id: Integer file id. Present as well as the uuid — the ACL rewrite keys on
            ``file_id`` and the tenant backfill on ``file_uuid`` (addendum **G5**), and a
            digest missing either becomes unreachable by one of them.
        digest: The stored ``file_facts.digest`` payload.
        facts: The stored ``file_facts.facts`` payload, for the roster and date.
        base_metadata: Per-file fields shared with chunk documents.

    Returns:
        One document per digest section, in section order.
    """
    roster = list(facts.get("roster") or [])
    recorded_at = facts.get("recorded_at")
    documents: list[dict[str, Any]] = []

    for section in digest.get("sections", []):
        index = int(section["index"])
        body = str(section["text"])
        document: dict[str, Any] = dict(base_metadata)
        document.update(
            {
                "file_id": file_id,
                "file_uuid": file_uuid,
                DOC_TYPE_FIELD: DOC_TYPE_DIGEST,
                "chunk_index": digest_chunk_index(index),
                "digest_section": index,
                "content": body,
                "embedding_text": build_embedding_text(
                    title=base_metadata.get("title"),
                    recorded_at=recorded_at,
                    roster=roster,
                    body=body,
                ),
                "speakers": list(section.get("speakers") or roster),
                "start_time": section.get("start_time"),
                "end_time": section.get("end_time"),
            }
        )
        # A digest is not attributable to one speaker; leaving the single-valued `speaker`
        # field unset keeps it out of the speaker facet and out of chat's speaker-scoped
        # `terms` filter, which is an exact match on a name the digest does not have.
        document.pop("speaker", None)
        documents.append(document)

    return documents


#: What Stage 3's ``_id`` argument must be, alongside the document. Kept next to the
#: builder so the two cannot drift.
def digest_document_ids(file_uuid: str, digest: dict[str, Any]) -> list[str]:
    """Ids matching :func:`build_digest_documents`, in the same order."""
    return [digest_document_id(file_uuid, int(s["index"])) for s in digest.get("sections", [])]
