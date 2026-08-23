"""The target index mapping Stage 3 will apply — pinned here, before it is applied.

#403 Stage 2's scope says the mapping is *defined* here and not applied, because Stage 3
gets exactly one ``_INDEX_VERSION`` bump and one reindex across every deployment. These
tests are the contract: they hold the shape still between the stage that decided it and
the stage that ships it, and each one names the addendum gap it exists for.

Nothing here talks to OpenSearch. ``tests/integration/test_embedding_window_truncation.py``
is the half that does.
"""

from __future__ import annotations

from typing import Any
from typing import cast

from app.services.ingest_artifacts import index_mapping as target
from app.services.ingest_artifacts import sizing
from app.services.ingest_artifacts.digest import build_digest
from app.services.search.indexing_service import _INDEX_VERSION
from app.services.search.indexing_service import TRANSCRIPT_CHUNKS_INDEX_BODY


def _digest():
    segments = [
        {
            "id": i + 1,
            "text": "We agreed to move the launch to November and revisit the marketing "
            "budget once engineering confirms the date.",
            "start_time": float(i * 6),
            "end_time": float(i * 6 + 6),
            "speaker": "Dana" if i % 2 else "Marcus",
        }
        for i in range(40)
    ]
    return build_digest(segments)


def test_the_fields_and_the_bump_landed_together():
    """Stage 3 applied it, and applied ALL of it in ONE bump.

    Until Stage 3 this test asserted the opposite — ``_INDEX_VERSION == 5`` and none of
    the fields present — because a bump with nothing writing the fields would have made
    every deployment's next full reindex delete and recreate the index for nothing. It
    is retargeted rather than deleted: the invariant it protects is not "Stage 2 has not
    shipped" but "the mapping and the version never disagree", and that is exactly as
    load-bearing afterwards. A field added to the live mapping without a bump reaches
    fresh installs only; a bump without the field costs every deployment a reindex.

    ``_INDEX_VERSION`` is assigned *from* ``TARGET_INDEX_VERSION``, so the numeric half
    cannot drift. The mapping half can, which is what the loop checks.
    """
    assert _INDEX_VERSION == target.TARGET_INDEX_VERSION
    mappings = cast(dict[str, Any], TRANSCRIPT_CHUNKS_INDEX_BODY["mappings"])
    properties = mappings["properties"]
    for field, definition in target.TARGET_MAPPING_ADDITIONS.items():
        assert properties.get(field) == definition, (
            f"{field} is missing from the live mapping (or differs from the pinned shape) "
            f"while _INDEX_VERSION is {_INDEX_VERSION} — one reindex was supposed to carry "
            f"all of it"
        )
    assert mappings["_meta"]["version"] == target.TARGET_INDEX_VERSION


def test_the_ingest_pipeline_embeds_the_contextualized_field():
    """The other half of the v6 bump, and the reason it needs the reindex.

    Repointing ``field_map`` changes what future documents embed; existing documents
    keep vectors built from the old field until they are rebuilt. #401 is what makes the
    repoint reach an upgraded deployment at all — the drift check compares ``field_map``,
    so a live pipeline is recreated rather than left silently embedding ``content``.
    """
    from app.services.search.indexing_service import _build_neural_ingest_pipeline

    processor = _build_neural_ingest_pipeline("model-1")["processors"][0]["text_embedding"]
    assert processor["field_map"] == {"embedding_text": "embedding"}
    assert "embedding_text" in target.TARGET_MAPPING_ADDITIONS, (
        "the pipeline reads a field the mapping does not declare"
    )


def test_doc_type_is_mapped_explicitly_as_keyword():
    """Addendum G2, with the cause stated correctly.

    Probed against OpenSearch 3.4: a *dynamically* mapped ``doc_type`` is ``text`` +
    ``.keyword`` and a bare ``term`` still matches all four planned values, because they
    are single lowercase tokens. So the explicit mapping is insurance against a future
    value or analyzer change, not a fix for a broken filter — and it does nothing at all
    for the documents already indexed without the field. That second problem is
    :func:`chunk_plane_clause`'s, and it is the one that matters.
    """
    mappings = cast(dict[str, Any], TRANSCRIPT_CHUNKS_INDEX_BODY["mappings"])
    assert mappings.get("dynamic") != "strict"
    assert target.TARGET_MAPPING_ADDITIONS[target.DOC_TYPE_FIELD] == {"type": "keyword"}


def test_a_bare_term_would_exclude_every_document_already_indexed():
    """The actual G2 hazard, as a property of the clauses rather than of OpenSearch.

    Chunk documents written before Stage 3 carry no ``doc_type``. A bare
    ``{"term": {"doc_type": "chunk"}}`` matches none of them — which is how the #400
    prune count silently returns 0 for an entire installed corpus. The compat arm is the
    control: it must match a legacy document, and the bare term must not.
    """
    legacy = {"file_uuid": "u", "chunk_index": 0, "content": "..."}
    assert "doc_type" not in legacy

    bare = {"term": {target.DOC_TYPE_FIELD: target.DOC_TYPE_CHUNK}}
    assert not _matches(bare, legacy), "a legacy document cannot satisfy a bare term"
    assert _matches(target.chunk_plane_clause(), legacy), (
        "the compat arm must match documents indexed before doc_type existed"
    )
    assert _matches(target.chunk_plane_clause(), dict(legacy, doc_type="chunk"))
    assert not _matches(target.chunk_plane_clause(), dict(legacy, doc_type="digest"))


def _matches(clause: dict, document: dict) -> bool:
    """Evaluate the two clause shapes this module builds against one document.

    Deliberately tiny and total: it understands ``term``, ``exists``, and the
    ``bool``/``should``/``must_not`` nesting used here, and raises on anything else so a
    change in clause shape fails loudly instead of quietly returning False.
    """
    if "term" in clause:
        ((field, value),) = clause["term"].items()
        return bool(document.get(field) == value)
    if "exists" in clause:
        return clause["exists"]["field"] in document
    if "bool" in clause:
        body = clause["bool"]
        if "must_not" in body:
            inner = body["must_not"]
            inner = inner if isinstance(inner, list) else [inner]
            if any(_matches(c, document) for c in inner):
                return False
        if "should" in body:
            needed = int(body.get("minimum_should_match", 1))
            return sum(_matches(c, document) for c in body["should"]) >= needed
        return True
    raise AssertionError(f"unhandled clause shape: {clause!r}")


def test_the_discriminator_is_doc_type_and_the_value_set_covers_documents():
    """#403 D1: ``doc_type`` wins over #362's ``source_type``. One field, one value set."""
    assert target.DOC_TYPE_FIELD == "doc_type"
    assert set(target.DOC_TYPES) == {
        "chunk",
        "digest",
    }
    assert set(target.VERBATIM_DOC_TYPES) == {"chunk"}


def test_the_chunk_plane_clause_still_matches_legacy_documents():
    """The single most likely production break in the plan, per the review.

    Existing indices have no ``doc_type``. ``_check_index_version`` only logs, so a v5
    index can run for months — and a bare ``term`` filter would exclude the whole corpus.
    """
    clause = target.chunk_plane_clause()["bool"]
    assert clause["minimum_should_match"] == 1
    assert {"term": {"doc_type": "chunk"}} in clause["should"]
    assert {"bool": {"must_not": {"exists": {"field": "doc_type"}}}} in clause["should"]


def test_the_digest_clause_has_no_compat_arm():
    """Digests are all new, so "absent" must never mean "digest"."""
    assert target.digest_plane_clause() == {"term": {"doc_type": "digest"}}


def test_digest_ids_cannot_collide_with_chunk_ids():
    """Addendum G2: chunk ids are ``{uuid}_{chunk_index}``, so ``{uuid}_0`` is taken."""
    uuid = "1f0a9e1c-0000-7000-8000-000000000001"
    digest_ids = {target.digest_document_id(uuid, n) for n in range(8)}
    chunk_ids = {f"{uuid}_{n}" for n in range(2000)}
    assert not (digest_ids & chunk_ids)


def test_the_chunk_index_sentinel_is_negative_and_unique_per_section():
    """``index.sort.field`` includes ``chunk_index``; a digest needs one that is not a
    position in the transcript."""
    sentinels = [target.digest_chunk_index(n) for n in range(8)]
    assert all(s < 0 for s in sentinels)
    assert len(set(sentinels)) == len(sentinels)
    assert sentinels == sorted(sentinels, reverse=True)


def test_a_digest_document_carries_both_file_id_and_file_uuid():
    """Addendum G5 — this one is a permission bug, not a relevance bug.

    ``update_file_access_index`` rewrites ``accessible_user_ids`` keyed on ``file_id``;
    the tenant backfill stamps ``organization_id`` keyed on ``file_uuid``. A digest
    missing either is unreachable by one of them, so a share revocation can leave a
    stale-ACL digest still retrievable.
    """
    documents = target.build_digest_documents(
        file_uuid="1f0a9e1c-0000-7000-8000-000000000001",
        file_id=42,
        digest=_digest(),
        facts={"roster": ["Dana", "Marcus"], "recorded_at": "2026-08-12T00:00:00+00:00"},
        base_metadata={"title": "Weekly sync", "user_id": 7, "accessible_user_ids": [7]},
    )
    assert documents
    for document in documents:
        assert document["file_id"] == 42
        assert document["file_uuid"] == "1f0a9e1c-0000-7000-8000-000000000001"
        assert document["accessible_user_ids"] == [7]


def test_a_digest_document_has_no_single_speaker_field():
    """A digest is not attributable to one speaker.

    Leaving ``speaker`` set would put digest text in the speaker facet and make chat's
    speaker-scoped ``terms`` filter (an exact match on a display name) match a document
    nobody said.
    """
    documents = target.build_digest_documents(
        file_uuid="u",
        file_id=1,
        digest=_digest(),
        facts={"roster": ["Dana"], "recorded_at": None},
        base_metadata={"title": "t", "speaker": "Dana"},
    )
    assert documents
    assert all("speaker" not in d for d in documents)
    assert all(d["speakers"] for d in documents)


def test_the_document_ids_line_up_with_the_documents():
    digest = _digest()
    documents = target.build_digest_documents(
        file_uuid="u", file_id=1, digest=digest, facts={"roster": []}, base_metadata={}
    )
    ids = target.digest_document_ids("u", digest)
    assert len(ids) == len(documents)
    assert ids == [target.digest_document_id("u", d["digest_section"]) for d in documents]


def test_the_composed_embedding_text_fits_the_measured_window():
    """The whole point of G8: header plus body must survive tokenisation intact."""
    digest = _digest()
    documents = target.build_digest_documents(
        file_uuid="u",
        file_id=1,
        digest=digest,
        facts={
            "roster": ["Dana", "Marcus", "Priya", "Sam", "Alex", "Robin"],
            "recorded_at": "2026-08-12T00:00:00+00:00",
        },
        base_metadata={"title": "Quarterly planning and budget review"},
    )
    assert len(documents) == len(digest["sections"]) >= 1, (
        "no digest documents to check — the loop below would pass vacuously"
    )
    for document in documents:
        assert target.embedding_text_fits(document["embedding_text"]), (
            f"{sizing.estimate_wordpieces(document['embedding_text'])} estimated wordpieces "
            f"exceeds the measured {sizing.EMBEDDING_CONTENT_WORDPIECES}-piece content budget"
        )


def test_a_huge_roster_is_elided_rather_than_eating_the_window():
    text = target.build_embedding_text(
        title="All hands",
        recorded_at="2026-08-12",
        roster=[f"Person{i}" for i in range(40)],
        body="We agreed to ship on Friday.",
    )
    assert "+34 more" in text
    assert target.embedding_text_fits(text)


def test_the_header_stays_inside_the_reserve_stage_2_sized_the_body_against():
    """If Stage 3's header grows past the reserve, digest text is silently clipped."""
    header_only = target.build_embedding_text(
        title="Quarterly planning and budget review",
        recorded_at="2026-08-12",
        roster=["Dana", "Marcus", "Priya", "Sam", "Alex", "Robin"],
        body="",
    )
    assert sizing.estimate_wordpieces(header_only) <= sizing.HEADER_WORDPIECE_RESERVE


def test_content_is_the_digest_text_so_bm25_scores_the_summary_not_the_header():
    """``content`` stays the BM25 field; the header rides ``embedding_text`` only.

    The plan is explicit that a per-file constant on every document lowers within-file
    discrimination, and that ``content`` is what lands in the prompt.
    """
    digest = _digest()
    documents = target.build_digest_documents(
        file_uuid="u",
        file_id=1,
        digest=digest,
        facts={"roster": ["Dana"], "recorded_at": "2026-08-12T00:00:00+00:00"},
        base_metadata={"title": "Weekly sync"},
    )
    assert len(documents) == len(digest["sections"]) >= 1, (
        "no digest documents to check — the loop below would pass vacuously"
    )
    for document, section in zip(documents, digest["sections"], strict=True):
        assert document["content"] == section["text"]
        assert "Weekly sync" not in document["content"]
        assert document["embedding_text"].startswith("Weekly sync | 2026-08-12")
