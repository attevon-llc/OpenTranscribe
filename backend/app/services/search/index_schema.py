"""Chunks-index schema: the index body, the MAJOR version and the additive (MINOR) steps."""

from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.services.ingest_artifacts import index_mapping as digest_mapping

# Index version -- bump when mappings or analysis settings change.
# Stored in index _meta so ensure_chunks_index_exists() can detect stale indices.
# v5: added organization_id (tenant scope) for cloud-edition isolation.
# v6: doc_type discriminator + digest documents + embedding_text (#403 Stage 3).
#     The mapping additions and the id/clause helpers live in
#     services/ingest_artifacts/index_mapping.py, which Stage 2 pinned so this
#     bump could carry all of them in ONE reindex.
_INDEX_VERSION = digest_mapping.TARGET_INDEX_VERSION

#: Read alias in front of ``OPENSEARCH_CHUNKS_INDEX``. Named once because its
#: creation and teardown are duplicated across ``ensure_chunks_index_exists`` and
#: ``recreate_index_for_dimension`` and must stay in sync — and the corruption
#: repair in ``opensearch_service/repair.py`` is now a third site that has to
#: tear it down before deleting the index behind it.
CHUNKS_ALIAS_NAME = "transcript_search"


@dataclass(frozen=True)
class AdditiveMappingStep:
    """One MINOR, non-destructive mapping change: new fields, nothing that could
    conflict with what is already indexed.

    Contrast with ``_INDEX_VERSION`` (MAJOR): that number names a change big
    enough to need a full reindex — a changed analyzer, a field whose TYPE
    changed, a vector dimension change — and the only tool this codebase has
    for landing one is ``recreate_index_for_dimension``, which **deletes the
    index**. An additive step is the opposite case: a brand-new field that no
    existing document has an opinion about, applied with ``indices.put_mapping``,
    which OpenSearch accepts against a live index with no downtime and no data
    loss. Old documents simply lack the field until they are next written —
    see the compat-arm rule at :func:`chunk_plane_clause` for why every reader
    of a NEW additive field must check ``exists`` rather than assume it.

    Attributes:
        version: The ``_meta.additive_version`` this step brings the index to.
            Steps apply in ascending order and each one is independently
            idempotent — OpenSearch's own mapping-merge semantics make PUTting
            an unchanged field definition a no-op, so replaying an already-
            applied step is safe by construction, not just by the version gate
            in :func:`_apply_pending_additive_steps`.
        description: One line, for the log line that records it happened.
        properties: The ``mappings.properties`` fragment this step adds.
    """

    version: int
    description: str
    properties: dict[str, Any]


#: Append here for the next additive field. Never edit a landed entry's
#: ``properties`` after it has shipped — that changes what step N meant on every
#: deployment that already applied it; add step N+1 instead.
ADDITIVE_MAPPING_STEPS: tuple[AdditiveMappingStep, ...] = (
    AdditiveMappingStep(
        version=1,
        description="speaker_id / profile_id integer fields for id-based speaker filtering",
        properties={
            # Plain integers, NOT `eager_global_ordinals` (unlike `speaker`/`tags`
            # above): that setting pre-builds a global ordinal map at refresh time
            # to speed up aggregations/sorts on a keyword field with many distinct
            # values, and an integer field gets no such option. These two exist to
            # be filtered on, not aggregated, and are lazy-backfilled (old
            # documents lack them entirely) — building eager global ordinals for a
            # sparse, mostly-integer field on every refresh would be pure cost for
            # a facet nothing reads yet.
            "speaker_id": {"type": "integer"},
            "profile_id": {"type": "integer"},
        },
    ),
    AdditiveMappingStep(
        version=2,
        description=(
            "page / section_path / char_start / char_end. RETIRED but RETAINED: nothing "
            "writes these today. The step must not be deleted and version 2 must never "
            "be reused — a deployed index already stamped `additive_version: 2` would "
            "then disagree with a freshly created one, and _apply_pending_additive_steps "
            "only ever moves the stamp forward, so the divergence would be permanent and "
            "silent. Four unused mapped fields cost nothing; a reused version number does."
        ),
        properties={
            "page": {"type": "integer"},
            "section_path": {"type": "keyword"},
            "char_start": {"type": "integer"},
            "char_end": {"type": "integer"},
        },
    ),
    AdditiveMappingStep(
        version=3,
        description="summary_key_path / summary_leaf_index for the summary plane (#963)",
        properties={
            # keyword, not text: this is a JSON path the frontend resolves verbatim
            # (frontend/src/lib/utils/summaryKeyPath.ts). Analyzing it would make it
            # unusable as an exact term and buy nothing — it is never searched.
            "summary_key_path": {"type": "keyword"},
            # The prune counter, sibling of `digest_section`. NOT `chunk_index`: a
            # summary's is a negative sentinel that counts the wrong way.
            "summary_leaf_index": {"type": "integer"},
        },
    ),
)

#: The highest step version — what a freshly created index is stamped with, and
#: what ``_apply_pending_additive_steps`` brings an existing index up to.
_ADDITIVE_VERSION = max((step.version for step in ADDITIVE_MAPPING_STEPS), default=0)

#: Union of every step's field additions, folded into the base mapping so a
#: FRESH index is created with them already present — only an EXISTING index
#: needs the incremental ``put_mapping`` walk.
_ADDITIVE_MAPPING_ADDITIONS: dict[str, dict[str, Any]] = {
    field: definition
    for step in ADDITIVE_MAPPING_STEPS
    for field, definition in step.properties.items()
}

# Index config for transcript chunks
#
# number_of_shards / number_of_replicas are env-tunable (OPENSEARCH_CHUNKS_INDEX_SHARDS /
# OPENSEARCH_CHUNKS_INDEX_REPLICAS) but ONLY take effect when the index is CREATED — OpenSearch
# does not let an existing index change its shard count in place, and this module never
# recreates the index to pick up a new value (that would mean deleting it; see
# recreate_index_for_dimension's docstring for why that path is reserved for a dimension
# change, and _apply_pending_additive_steps's for why the additive path never reaches it).
# Defaults (1 shard, 0 replicas) are the shipped single-node topology and are pinned by
# tests/unit/test_index_topology.py — do not change them here.
TRANSCRIPT_CHUNKS_INDEX_BODY = {
    "settings": {
        "index": {
            "knn": True,
            "number_of_shards": settings.OPENSEARCH_CHUNKS_INDEX_SHARDS,
            "number_of_replicas": settings.OPENSEARCH_CHUNKS_INDEX_REPLICAS,
            "sort.field": ["file_uuid", "chunk_index"],
            "sort.order": ["asc", "asc"],
        },
        "analysis": {
            "analyzer": {
                "transcript": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": [
                        "lowercase",
                        "english_stop",
                        "english_snowball",
                        "shingle_filter",
                    ],
                }
            },
            "filter": {
                "english_stop": {"type": "stop", "stopwords": "_english_"},
                "english_snowball": {"type": "snowball", "language": "English"},
                "shingle_filter": {
                    "type": "shingle",
                    "min_shingle_size": 2,
                    "max_shingle_size": 3,
                    "output_unigrams": True,
                },
            },
        },
    },
    "mappings": {
        "_meta": {
            "version": _INDEX_VERSION,
            # MINOR schema counter (see AdditiveMappingStep). A freshly created
            # index is stamped at the latest step; ensure_chunks_index_exists()
            # walks an EXISTING index up to it via put_mapping.
            "additive_version": _ADDITIVE_VERSION,
        },
        "properties": {
            # Identity
            "file_id": {"type": "integer"},
            "file_uuid": {"type": "keyword"},
            "user_id": {"type": "integer"},
            # Tenant scope (cloud-edition seam; absent on community/personal docs)
            "organization_id": {"type": "integer"},
            "chunk_index": {"type": "integer"},
            # Content (BM25 searchable)
            "content": {
                "type": "text",
                "analyzer": "transcript",
                "fields": {"exact": {"type": "text", "analyzer": "standard"}},
            },
            "title": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            # Metadata (filterable)
            "speaker": {"type": "keyword", "eager_global_ordinals": True},
            "speakers": {"type": "keyword"},
            "tags": {"type": "keyword", "eager_global_ordinals": True},
            "content_type": {"type": "keyword"},
            "duration": {"type": "float"},
            "file_size": {"type": "long"},
            "collection_ids": {"type": "integer"},
            "accessible_user_ids": {"type": "integer"},
            "upload_time": {"type": "date"},
            "language": {"type": "keyword"},
            # Timestamps (for video navigation)
            "start_time": {"type": "float"},
            "end_time": {"type": "float"},
            # Vector embedding
            "embedding": {
                "type": "knn_vector",
                "dimension": 384,  # Updated dynamically at index creation time
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "lucene",
                    "parameters": {
                        "ef_construction": 256,
                        "m": 16,
                    },
                },
            },
            # Tracking
            "embedding_model": {"type": "keyword"},
            "indexed_at": {"type": "date"},
            # v6 (#403 Stage 3): the doc_type discriminator, the field the neural
            # ingest pipeline embeds, and the digest section number. Defined in
            # services/ingest_artifacts/index_mapping.py so Stage 2 could pin the
            # shape and Stage 3 could apply it unchanged.
            **digest_mapping.TARGET_MAPPING_ADDITIONS,
            # Additive (MINOR) fields — see ADDITIVE_MAPPING_STEPS. Folded in here
            # so a FRESH index already has them; an existing index gets them via
            # _apply_pending_additive_steps instead of a version bump + reindex.
            **_ADDITIVE_MAPPING_ADDITIONS,
        },
    },
}
