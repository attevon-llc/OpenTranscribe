"""Transcript indexing service for OpenSearch chunk-level search."""

import contextlib
import datetime
import logging
import secrets
import time
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.services.ingest_artifacts import index_mapping as digest_mapping
from app.services.opensearch_service import get_opensearch_client
from app.services.opensearch_service import opensearch_client

from .chunking_service import chunk_transcript_by_speaker_turns
from .embedding_provenance import active_embedding_model
from .embedding_provenance import reset_active_embedding_model
from .embedding_provenance import resolve_model_label
from .embedding_provenance import set_active_embedding_model
from .fusion import FusionConfig
from .fusion import pipeline_matches
from .fusion import resolve_fusion
from .fusion import search_pipeline_id

logger = logging.getLogger(__name__)

# Track whether neural pipeline is available
_neural_pipeline_verified = False
_neural_pipeline_available = False

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

# Transient bulk error types that are safe to retry
_RETRYABLE_ERROR_TYPES = frozenset(
    {
        "es_rejected_execution_exception",
        "circuit_breaking_exception",
        "cluster_block_exception",
    }
)

#: Retry budget for the transient bulk errors above (#495 follow-on).
#:
#: This was 2 attempts at 1 s and 2 s, and it was too short for every condition the
#: set above names. MEASURED: the ML Commons breaker
#: (``circuit_breaking_exception: Memory Circuit Breaker is open``) failed **8 of 8
#: documents twice through the full 1 s + 2 s budget** — because it trips on
#: *instantaneous* JVM heap-used, which under bulk load is dominated by uncollected
#: young-generation garbage and stays high until G1 runs. The same node measured
#: ``old gen 382 MB`` of a 4 GB heap: a false trip, not exhaustion.
#:
#: `ml_model_service.configure_ml_settings` raises the threshold that caused it, so
#: this is the second line of defence rather than the fix. It still matters: a
#: rejected-execution or a closed index (both in the set above) also outlast three
#: seconds, and the alternative is failing the task.
#:
#: Jitter is not decoration. Every worker indexing when a shared cluster resource
#: goes over its limit retries on the same schedule, so a fixed backoff has them all
#: return together and re-trip it — the retry becomes the load.
_BULK_RETRY_ATTEMPTS = 4
_BULK_RETRY_BASE_SECONDS = 1.0
_BULK_RETRY_MAX_SECONDS = 8.0

#: How many consecutive document ids one realtime orphan probe covers (#435).
#:
#: A bulk load writes ``0..n-1``, so the tail a shorter re-chunk orphans is normally
#: contiguous and a single id would answer the question. The probe is a *window*
#: because that assumption is not guaranteed and is already violated on purpose in
#: the suite: ``_extract_failed_docs`` drops documents whose bulk error is permanent,
#: leaving a hole, and ``test_a_shorter_resection_leaves_no_orphan_digest`` plants its
#: orphan at ``sections + 3``. 64 ids cost one round trip.
#:
#: ⚠️ **An empty window does not end the walk.** It used to, and a hole of 64 or more
#: therefore stranded every document above it — permanently, which is the exact
#: failure #435 exists to prevent, reintroduced by the stop condition itself. The
#: walk now consults ``_probe_ceiling`` before giving up; see
#: :meth:`TranscriptIndexingService._orphaned_document_ids`.
_ORPHAN_PROBE_WINDOW = 64

# Permanent error types that should NOT be retried
_PERMANENT_ERROR_TYPES = frozenset(
    {
        "mapper_parsing_exception",
        "strict_dynamic_mapping_exception",
        "illegal_argument_exception",
    }
)

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


def _invalidate_chat_retrieval_cache() -> None:
    """Mark the searchable corpus as changed so chat can't serve stale passages.

    RAG chat caches retrieval results for a few minutes. Without this, for the
    length of that window an answer could quote a recording that was just
    deleted, quarantined or re-transcribed — and cite a link that no longer
    resolves. Contained: indexing must never fail because a cache marker
    could not be written.
    """
    try:
        from app.services.chat.retrieval_cache import bump_corpus_version

        bump_corpus_version()
    except Exception:  # noqa: BLE001
        logger.debug("Could not bump chat corpus version", exc_info=True)


def chunk_plane_query(
    file_uuid: str,
    *,
    from_chunk_index: int | None = None,
    extra_filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the query that selects the CHUNK-plane documents of one file.

    **Every delete and every targeted rewrite against the chunks index is built
    here, or in its two siblings below, and nowhere else.**
    Since v6 the index also holds per-file **digest** documents
    (``doc_type: digest``), so a predicate matching only on ``file_uuid`` takes
    the digests with it — which is why the discriminator lives here rather than
    at each call site.

    The clause is :func:`~app.services.ingest_artifacts.index_mapping.chunk_plane_clause`,
    not a bare ``{"term": {"doc_type": "chunk"}}``, and the difference is the
    whole point: **every chunk written before v6 carries no ``doc_type`` at
    all**, so a bare term matches none of them and the #400 stale-tail prune
    silently stops working for an entire installed corpus. An explicit mapping
    does nothing for documents written before it existed.

    ``extra_filters`` exists so a caller that needs to narrow *within* the chunk
    plane (the rename propagation of issue #405 only wants the docs still
    carrying the old speaker name) still inherits whatever predicate this
    function grows.

    Args:
        file_uuid: UUID of the media file whose chunks are being selected.
        from_chunk_index: When set, restrict to chunks at or above this index —
            the stale tail left behind by a longer previous chunking.
        extra_filters: Additional ``filter`` clauses ANDed onto the predicate.
            Express an OR as one nested ``bool``/``should`` clause.

    Returns:
        An OpenSearch query body fragment (the value of ``"query"``).
    """
    filters: list[dict[str, Any]] = [
        {"term": {"file_uuid": file_uuid}},
        digest_mapping.chunk_plane_clause(),
    ]
    if from_chunk_index is not None:
        filters.append({"range": {"chunk_index": {"gte": from_chunk_index}}})
    if extra_filters:
        filters.extend(extra_filters)
    return {"bool": {"filter": filters}}


def digest_plane_query(
    file_uuid: str,
    *,
    from_section: int | None = None,
) -> dict[str, Any]:
    """The digest-plane sibling of :func:`chunk_plane_query`.

    No compatibility arm: digest documents are all new, so ``doc_type`` is
    always present on them.

    Args:
        file_uuid: UUID of the media file whose digest sections are selected.
        from_section: When set, restrict to sections at or above this index —
            the orphans left behind when a digest re-sections to fewer parts,
            exactly the way a shorter re-chunk orphans a chunk tail (#400).

    Returns:
        An OpenSearch query body fragment.
    """
    filters: list[dict[str, Any]] = [
        {"term": {"file_uuid": file_uuid}},
        digest_mapping.digest_plane_clause(),
    ]
    if from_section is not None:
        filters.append({"range": {"digest_section": {"gte": from_section}}})
    return {"bool": {"filter": filters}}


def file_plane_query(file_uuid: str) -> dict[str, Any]:
    """**Every** document of one file, whatever plane it belongs to.

    Deliberately has no ``doc_type`` predicate, and all three callers need that:
    deleting a media file must leave nothing behind, a full rebuild wants a clean
    slate before it regenerates both planes, and
    :meth:`TranscriptIndexingService.count_file_documents` verifies the delete
    against the same predicate the delete used. Using :func:`chunk_plane_query`
    for any of them would strand the digests — readable, with whatever ACL they
    were last stamped with.

    Args:
        file_uuid: UUID of the media file.

    Returns:
        An OpenSearch query body fragment.
    """
    return {"bool": {"filter": [{"term": {"file_uuid": file_uuid}}]}}


def _build_hybrid_search_pipeline() -> dict[str, Any]:
    """Build the RRF search pipeline configuration with configurable rank_constant.

    Lower rank_constant values give more weight to top-ranked results.
    ``SEARCH_RRF_RANK_CONSTANT`` defaults to 30, tuned for transcript search (shorter
    queries, focused collections). The standard value of 60 from the original RRF
    paper is optimized for web search.
    """
    return {
        "description": "Hybrid BM25 + vector search with RRF",
        "phase_results_processors": [
            {
                "score-ranker-processor": {
                    "combination": {
                        "technique": "rrf",
                        "rank_constant": settings.SEARCH_RRF_RANK_CONSTANT,
                    }
                }
            }
        ],
    }


def ensure_chunks_index_exists() -> bool:
    """Ensure the transcript chunks index exists with proper kNN config.

    Returns:
        True if index exists or was created, False on error.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    try:
        if opensearch_client.indices.exists(index=index_name):
            # Check MAJOR index version from _meta (log-only; a real upgrade
            # needs a full reindex, never done here).
            _check_index_version(index_name)
            # Apply any pending MINOR (additive) mapping steps. Idempotent and
            # non-destructive — see _apply_pending_additive_steps. This is the
            # ONLY thing standing between "add a field" and "bump _INDEX_VERSION
            # and cost every deployment a full reindex" for a change that does
            # not actually need one.
            _apply_pending_additive_steps(index_name)
            return True

        # Get dimension from settings service (reads from DB with default fallback)
        from app.services.search.settings_service import get_search_embedding_dimension

        dimension = get_search_embedding_dimension()
        index_body = _get_index_body_with_dimension(dimension)

        opensearch_client.indices.create(index=index_name, body=index_body)
        logger.info(f"Created transcript chunks index: {index_name} (version={_INDEX_VERSION})")

        # Create alias
        alias_name = CHUNKS_ALIAS_NAME
        if not opensearch_client.indices.exists_alias(name=alias_name):
            opensearch_client.indices.put_alias(index=index_name, name=alias_name)
            logger.info(f"Created alias {alias_name} -> {index_name}")

        return True
    except Exception as e:
        logger.error(f"Error creating chunks index: {e}")
        return False


def recreate_index_for_dimension(dimension: int) -> bool:
    """Recreate the chunks index if the embedding dimension has changed.

    This is called during model switch to ensure the index mapping matches
    the new model's vector dimension. The old index is deleted and a new
    one is created with the correct dimension.

    Args:
        dimension: The new embedding vector dimension.

    Returns:
        True if index was recreated or already correct, False on error.
    """
    if not opensearch_client:
        return False

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    try:
        if opensearch_client.indices.exists(index=index_name):
            # Check current dimension from mapping
            mapping = opensearch_client.indices.get_mapping(index=index_name)
            current_dim = (
                mapping.get(index_name, {})
                .get("mappings", {})
                .get("properties", {})
                .get("embedding", {})
                .get("dimension", 0)
            )

            if current_dim == dimension:
                logger.info(
                    f"Index {index_name} already has dimension {dimension}, no recreation needed"
                )
                return True

            logger.info(
                f"Dimension mismatch: index has {current_dim}, need {dimension}. Recreating index."
            )

            # Remove alias first if it exists
            alias_name = CHUNKS_ALIAS_NAME
            try:
                if opensearch_client.indices.exists_alias(name=alias_name):
                    opensearch_client.indices.delete_alias(index=index_name, name=alias_name)
            except Exception as e:
                logger.debug(f"Could not remove alias {alias_name} before index recreation: {e}")

            # Delete the old index
            opensearch_client.indices.delete(index=index_name)
            logger.info(f"Deleted old index {index_name}")

        # Create with new dimension
        index_body = _get_index_body_with_dimension(dimension)
        opensearch_client.indices.create(index=index_name, body=index_body)
        logger.info(f"Created index {index_name} with dimension {dimension}")

        # Recreate alias
        alias_name = CHUNKS_ALIAS_NAME
        opensearch_client.indices.put_alias(index=index_name, name=alias_name)
        logger.info(f"Created alias {alias_name} -> {index_name}")

        return True
    except Exception as e:
        logger.error(f"Error recreating index for dimension {dimension}: {e}")
        return False


def ensure_search_pipeline_exists(fusion: FusionConfig | None = None) -> bool:
    """Ensure the search pipeline for ``fusion`` exists with the right processors.

    Self-heals on drift: a pipeline whose stored body differs from the wanted one
    is deleted and recreated. Since #363 the comparison is **structural over the
    whole processor block** rather than a single ``rank_constant`` field, so a
    normalization pipeline whose technique changed heals the same way an RRF
    pipeline whose rank constant changed always has. OpenSearch echoes a pipeline
    body back verbatim, so an exact comparison is safe.

    Args:
        fusion: The strategy this pipeline implements; None for the configured
            default (RRF at ``SEARCH_RRF_RANK_CONSTANT``, the historical
            ``transcript-hybrid-search``).

    Returns:
        True if pipeline exists or was created, False on error.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    cfg = resolve_fusion(fusion)
    pipeline_id = search_pipeline_id(cfg)
    pipeline_body = cfg.pipeline_body()
    try:
        try:
            response = opensearch_client.transport.perform_request(
                "GET", f"/_search/pipeline/{pipeline_id}"
            )
            existing = response.get(pipeline_id, response) if isinstance(response, dict) else {}
            if pipeline_matches(existing, cfg):
                return True
            logger.info(f"Search pipeline {pipeline_id} drifted from {cfg.slug()}, recreating")
            opensearch_client.transport.perform_request(
                "DELETE", f"/_search/pipeline/{pipeline_id}"
            )
        except Exception:
            logger.debug(f"Search pipeline {pipeline_id} not found, will create it")

        # Create pipeline
        opensearch_client.transport.perform_request(
            "PUT",
            f"/_search/pipeline/{pipeline_id}",
            body=pipeline_body,
        )
        logger.info(f"Created search pipeline: {pipeline_id} ({cfg.slug()})")
        return True
    except Exception as e:
        logger.error(f"Error creating search pipeline: {e}")
        return False


def _get_model_id_from_service() -> str | None:
    """Get the active model ID from the ML model service.

    Returns:
        Model ID string or None if not available.
    """
    try:
        from .ml_model_service import get_ml_model_service

        ml_service = get_ml_model_service()
        return ml_service.get_active_model_id()
    except Exception as e:
        logger.warning(f"Could not get active model: {e}")
        return None


def _build_neural_ingest_pipeline(model_id: str) -> dict[str, Any]:
    """Build the neural ingest pipeline body for a model.

    The single definition of what the ingest pipeline should look like — both the
    creation path and the drift check below read it, so the two can never disagree
    about what "correct" is.

    Args:
        model_id: OpenSearch ML model ID that generates the embeddings.

    Returns:
        The pipeline body to PUT.
    """
    return {
        "description": f"Neural embedding pipeline for transcript search (model: {model_id})",
        "processors": [
            {
                "text_embedding": {
                    "model_id": model_id,
                    # v6: embed `embedding_text`, not `content` (#403 Stage 3).
                    # Every document carries it; for a chunk it is the file
                    # header plus the chunk text, for a digest section the same
                    # header plus the section. That header is the zero-LLM
                    # contextualization — it is what makes "the logistics team's
                    # retro" retrievable when the phrase is in the title and not
                    # in anything anybody said. #401 is what makes this reach
                    # upgraded deployments: the drift check compares field_map,
                    # so an existing pipeline is recreated rather than silently
                    # left embedding the old field.
                    "field_map": {"embedding_text": "embedding"},
                    "batch_size": settings.SEARCH_NEURAL_BATCH_SIZE,
                    "ignore_failure": False,
                }
            }
        ],
    }


def _check_existing_pipeline_config(pipeline_id: str, expected: dict[str, Any]) -> bool | None:
    """Check whether the live ingest pipeline matches the config we would write.

    Compares ``model_id``, ``field_map`` **and** ``batch_size`` — not just the model
    (issue #401). Comparing only the model meant a release that repointed
    ``field_map`` (say ``content`` → ``embedding_text``) took effect on fresh installs
    only: an upgraded deployment kept the old pipeline and silently kept embedding the
    old field, with no error, no log and no metric to show for it. This mirrors what
    ``ensure_search_pipeline_exists`` already does for the *search* pipeline, and
    recreating an ingest pipeline is cheap and non-destructive (existing documents keep
    their embeddings; only future ingests change — a ``field_map`` change still needs a
    reindex for old documents to pick up the new source field).

    ``batch_size`` is compared **only when the live pipeline carries one**: the creation
    path drops it and retries on OpenSearch versions that reject the parameter, and
    treating that absence as drift would recreate the pipeline on every single boot.

    ``ignore_failure`` is deliberately not compared — OpenSearch may or may not echo a
    false-valued flag back, and a spurious mismatch there would be the same boot loop.

    The processor list's **shape** is compared too: exactly one processor, and it a
    ``text_embedding``. Iterating to the first ``text_embedding`` and returning made an
    extra or reordered processor permanently invisible — see the comment at the check.

    Args:
        pipeline_id: Pipeline ID to check.
        expected: The ``text_embedding`` processor config we would write.

    Returns:
        True if the live pipeline matches, False if it has drifted, None if not found.
    """
    if not opensearch_client:
        return None

    try:
        response = opensearch_client.ingest.get_pipeline(id=pipeline_id)
    except Exception:
        logger.debug(f"Neural ingest pipeline {pipeline_id} not found, will create it")
        return None

    current_pipeline = response.get(pipeline_id, {})
    processors = current_pipeline.get("processors", [])

    # The SHAPE of the processor list is compared before its contents (#401
    # follow-up). This loop used to `continue` past every non-`text_embedding`
    # processor and `return` on the first one it found, so two kinds of drift were
    # invisible and permanent:
    #
    #   * an EXTRA processor — a stray `set`/`remove`, a second `text_embedding`
    #     for another field, anything left behind by a manual PUT or an older
    #     release. `_build_neural_ingest_pipeline` writes exactly ONE processor, so
    #     any second one is by definition drift, and it survived every boot;
    #   * a REORDERING — two processors swapped is a different program with an
    #     identical verdict.
    #
    # This is the rule `services/search/CLAUDE.md` already states for the sibling
    # SEARCH pipeline ("compares the whole processor block ... OpenSearch echoes a
    # pipeline body back verbatim, so the comparison is exact"). The ingest
    # pipeline simply never adopted it.
    if len(processors) != 1 or "text_embedding" not in processors[0]:
        kinds = [next(iter(processor), "?") for processor in processors]
        logger.info(
            f"Neural ingest pipeline {pipeline_id} has drifted, recreating "
            f"(expected exactly one text_embedding processor, found {kinds})"
        )
        return False

    for processor in processors:
        if "text_embedding" not in processor:
            continue

        live = processor["text_embedding"]
        drift = [
            f"{key}: {live.get(key)!r} != {expected.get(key)!r}"
            for key in ("model_id", "field_map")
            if live.get(key) != expected.get(key)
        ]
        if "batch_size" in live and live["batch_size"] != expected.get("batch_size"):
            drift.append(f"batch_size: {live['batch_size']!r} != {expected.get('batch_size')!r}")

        if drift:
            # A ``model_id`` drift is categorically worse than the other two and is
            # logged as such (issue #437): recreating the pipeline makes every FUTURE
            # document use the new model while every existing document keeps vectors
            # from the old one, and cosine between the two populations is meaningless.
            # The other drifts change what is embedded, not what embeds it.
            if live.get("model_id") != expected.get("model_id"):
                logger.warning(
                    f"Neural ingest pipeline {pipeline_id} is pointed at a DIFFERENT "
                    f"embedding model ({live.get('model_id')!r} -> "
                    f"{expected.get('model_id')!r}). Documents already indexed keep "
                    f"their old vectors; a FULL reindex is required before the index "
                    f"is a single comparable vector space again."
                )
            logger.info(
                f"Neural ingest pipeline {pipeline_id} has drifted, recreating ({'; '.join(drift)})"
            )
            return False
        return True

    return None


def ensure_neural_ingest_pipeline(model_id: str | None = None) -> bool:
    """Ensure the neural ingest pipeline exists with the specified model.

    The neural ingest pipeline uses OpenSearch's text_embedding processor
    to generate embeddings server-side during document ingestion.

    Args:
        model_id: OpenSearch ML model ID. If None, attempts to get from service.

    Returns:
        True if pipeline exists or was created, False on error.
    """
    global _neural_pipeline_verified, _neural_pipeline_available

    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        logger.debug("Neural search disabled, skipping neural ingest pipeline")
        return False

    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    pipeline_id = settings.OPENSEARCH_NEURAL_PIPELINE

    # Get model_id if not provided
    if not model_id:
        model_id = _get_model_id_from_service()

    if not model_id:
        logger.warning("No model_id available for neural ingest pipeline")
        return False

    try:
        pipeline_body = _build_neural_ingest_pipeline(model_id)
        text_embedding_config: dict[str, Any] = pipeline_body["processors"][0]["text_embedding"]
        batch_size = text_embedding_config["batch_size"]

        # Check whether the live pipeline still matches the whole config, not just
        # the model id (issue #401).
        pipeline_check = _check_existing_pipeline_config(pipeline_id, text_embedding_config)
        if pipeline_check is True:
            _neural_pipeline_verified = True
            _neural_pipeline_available = True
            set_active_embedding_model(resolve_model_label(model_id))
            return True

        # Create or update pipeline (try with batch_size first, fall back without)
        try:
            opensearch_client.ingest.put_pipeline(id=pipeline_id, body=pipeline_body)
            logger.info(
                f"Created/updated neural ingest pipeline: {pipeline_id} "
                f"with model {model_id} (batch_size={batch_size})"
            )
        except Exception as batch_err:
            logger.warning(
                f"Neural pipeline creation with batch_size={batch_size} failed: {batch_err}. "
                f"Retrying without batch_size."
            )
            # Fall back to pipeline without batch_size for older OpenSearch versions
            text_embedding_config.pop("batch_size", None)
            opensearch_client.ingest.put_pipeline(id=pipeline_id, body=pipeline_body)
            logger.info(
                f"Created/updated neural ingest pipeline: {pipeline_id} "
                f"with model {model_id} (no batch_size)"
            )

        _neural_pipeline_verified = True
        _neural_pipeline_available = True
        # Provenance is resolved HERE, from the model id we just wrote into the
        # pipeline, and nowhere else (#437). Reading it from
        # ``get_search_embedding_settings()`` instead would attach a label that is
        # demonstrably able to name a model that never touched the vector: the two
        # SystemSettings keys behind that function are written by different
        # endpoints and nothing reconciles them.
        set_active_embedding_model(resolve_model_label(model_id))
        return True

    except Exception as e:
        logger.error(f"Error creating neural ingest pipeline: {e}")
        _neural_pipeline_verified = False
        _neural_pipeline_available = False
        reset_active_embedding_model()
        return False


def is_neural_pipeline_available() -> bool:
    """Check if the neural ingest pipeline is available.

    Returns:
        True if neural pipeline is configured and available.
    """
    global _neural_pipeline_verified, _neural_pipeline_available

    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        return False

    if _neural_pipeline_verified:
        return _neural_pipeline_available

    # Try to verify
    return ensure_neural_ingest_pipeline()


def reset_neural_pipeline_state() -> None:
    """Reset the neural pipeline verification state.

    Call this when switching models or after configuration changes.
    """
    global _neural_pipeline_verified, _neural_pipeline_available
    _neural_pipeline_verified = False
    _neural_pipeline_available = False
    # The provenance label describes the pipeline, so it expires with it. Leaving
    # the previous model's name behind would stamp it on documents the NEXT model
    # embeds — a wrong label is worse than no label, because it is believed.
    reset_active_embedding_model()


def _get_index_body_with_dimension(dimension: int) -> dict[str, Any]:
    """Get index body with the correct embedding dimension."""
    import copy

    body: dict[str, Any] = copy.deepcopy(TRANSCRIPT_CHUNKS_INDEX_BODY)
    body["mappings"]["properties"]["embedding"]["dimension"] = dimension
    return body


def _check_index_version(index_name: str) -> None:
    """Check the index version stored in _meta and log a warning if outdated.

    Args:
        index_name: Name of the index to check.
    """
    if not opensearch_client:
        return

    try:
        mapping = opensearch_client.indices.get_mapping(index=index_name)
        meta = mapping.get(index_name, {}).get("mappings", {}).get("_meta", {})
        stored_version = meta.get("version", 0)

        if stored_version < _INDEX_VERSION:
            logger.warning(
                f"Index '{index_name}' is version {stored_version}, "
                f"latest is {_INDEX_VERSION}. "
                f"Run a full reindex to pick up mapping and analyzer changes."
            )
        elif stored_version == _INDEX_VERSION:
            logger.debug(f"Index '{index_name}' is at current version {_INDEX_VERSION}")
    except Exception as e:
        logger.debug(f"Could not check index version for {index_name}: {e}")


def _apply_pending_additive_steps(index_name: str) -> None:
    """Bring an EXISTING index's additive (MINOR) schema up to date, in place.

    This is the machinery that makes "add a field" not cost a reindex.
    ``_check_index_version`` above only *logs* a MAJOR-version drift — nothing
    upgrades a live index's analyzer or vector dimension without a full
    ``recreate_index_for_dimension`` (which deletes it). An additive field is a
    different kind of change: no existing document has an opinion about a field
    it has never seen, so OpenSearch's ``indices.put_mapping`` can add one to a
    live index with no downtime, no delete, and no reindex.

    **Idempotent by construction, twice over.** The version comparison below
    skips work once ``additive_version`` already meets the target, so a second
    call in the same process is a single ``get_mapping`` and nothing else. And
    even without that gate, PUTting an unchanged field definition is itself a
    no-op to OpenSearch's mapping-merge — so calling this against an index that
    somehow already has the fields (say, from a hand-run ``put_mapping``) is
    still safe; only a field whose type genuinely CHANGED would error, and that
    is precisely a case this mechanism is not for (bump ``_INDEX_VERSION``
    instead).

    **Deliberately never calls ``recreate_index_for_dimension``.** That
    function deletes and recreates the index; nothing an additive step needs
    to do requires that, and reaching for it here would turn a live,
    zero-downtime field addition into the exact destructive operation this
    machinery exists to avoid. See
    ``tests/unit/test_index_additive_version.py`` for the structural guard.

    Args:
        index_name: The chunks index to check and, if needed, update.
    """
    if not opensearch_client:
        return

    try:
        mapping = opensearch_client.indices.get_mapping(index=index_name)
        meta = mapping.get(index_name, {}).get("mappings", {}).get("_meta", {}) or {}
        current_additive = int(meta.get("additive_version", 0) or 0)

        pending = [step for step in ADDITIVE_MAPPING_STEPS if step.version > current_additive]
        if not pending:
            logger.debug(
                f"Index '{index_name}' additive schema is current "
                f"(additive_version={current_additive})"
            )
            return

        for step in pending:
            opensearch_client.indices.put_mapping(
                index=index_name, body={"properties": step.properties}
            )
            logger.info(
                f"Applied additive mapping step {step.version} to '{index_name}': "
                f"{step.description}"
            )

        # Preserve whatever MAJOR version is already stored — this call must
        # never claim the index reached a major version it did not actually
        # reindex to. Only additive_version moves.
        new_additive_version = max(step.version for step in pending)
        opensearch_client.indices.put_mapping(
            index=index_name,
            body={
                "_meta": {
                    "version": meta.get("version", 0),
                    "additive_version": new_additive_version,
                }
            },
        )
        logger.info(f"Index '{index_name}' additive_version now {new_additive_version}")
    except Exception as e:
        logger.error(f"Could not apply pending additive mapping steps to '{index_name}': {e}")


def survey_speaker_id_coverage() -> dict[str, Any]:
    """Measure what fraction of the CHUNK plane carries the new ``speaker_id`` field.

    This is the instrument a future "flip the speaker filter from names to ids"
    decision would read before proposing it (services/search/CLAUDE.md's rule for a
    measured change, same shape as ``embedding_provenance.survey_embedding_models``).
    It does **not** decide anything itself — ``speaker_id``/``profile_id`` are written
    going forward and backfilled lazily (rename propagation, per-file reindex, the
    opt-in maintenance pass), so coverage starts near 0% on any existing deployment
    and climbs only as documents are naturally rewritten.

    **Deliberately not built on the ``_get_indexed_uuids`` shape.** That function's
    ``terms`` aggregation on ``file_uuid`` has a hard 50,000-bucket ceiling — past
    that many distinct files it silently truncates and undercounts. This survey
    never enumerates distinct values at all: a ``filter`` aggregation returns one
    bounded ``doc_count``, correct at any corpus size.

    Returns:
        ``{"verdict": ..., "total": int, "with_speaker_id": int, "coverage_ratio": float}``.
        ``verdict`` is ``"unavailable"`` (no client, no index, or the query failed —
        never read as "zero coverage"), ``"empty"`` (index exists, chunk plane has
        no documents), or ``"measured"``.
    """
    empty_result: dict[str, Any] = {
        "verdict": "unavailable",
        "total": 0,
        "with_speaker_id": 0,
        "coverage_ratio": 0.0,
    }
    if not opensearch_client:
        return empty_result

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    try:
        if not opensearch_client.indices.exists(index=index_name):
            return empty_result

        response = opensearch_client.search(
            index=index_name,
            body={
                "size": 0,
                "track_total_hits": True,
                "query": {"bool": {"filter": [digest_mapping.chunk_plane_clause()]}},
                "aggs": {"with_speaker_id": {"filter": {"exists": {"field": "speaker_id"}}}},
            },
        )
    except Exception as e:
        logger.debug(f"Could not survey speaker_id coverage: {e}")
        return empty_result

    total = int((response.get("hits") or {}).get("total", {}).get("value", 0) or 0)
    if total == 0:
        return {"verdict": "empty", "total": 0, "with_speaker_id": 0, "coverage_ratio": 0.0}

    with_speaker_id = int(
        (response.get("aggregations") or {}).get("with_speaker_id", {}).get("doc_count", 0) or 0
    )
    return {
        "verdict": "measured",
        "total": total,
        "with_speaker_id": with_speaker_id,
        "coverage_ratio": round(with_speaker_id / total, 4),
    }


@contextlib.contextmanager
def _suspended_refresh_for_large_index(
    index_name: str,
    *,
    chunk_count: int,
    threshold: int,
) -> Iterator[None]:
    """Suspend an index's ``refresh_interval`` while a bulk load runs.

    Only triggers when ``chunk_count >= threshold`` — small transcripts
    still benefit from near-real-time search. On exit the prior interval
    is restored and a manual refresh is issued so the newly loaded
    chunks become searchable immediately.

    Args:
        index_name: Target OpenSearch index.
        chunk_count: Number of chunks about to be loaded.
        threshold: Minimum chunk count that activates the suspension.
    """
    if chunk_count < threshold or not opensearch_client:
        yield
        return

    prior_interval: str | None = None
    try:
        settings_resp = opensearch_client.indices.get_settings(index=index_name)
        prior_interval = (
            settings_resp.get(index_name, {})
            .get("settings", {})
            .get("index", {})
            .get("refresh_interval")
        )
    except Exception as e:
        logger.debug(f"Could not read refresh_interval for {index_name}, skipping suspension: {e}")
        yield
        return

    try:
        opensearch_client.indices.put_settings(
            index=index_name, body={"index": {"refresh_interval": "-1"}}
        )
        logger.info(
            f"Suspended refresh on {index_name} for bulk load of {chunk_count} chunks "
            f"(prior refresh_interval={prior_interval!r})"
        )
        yield
    finally:
        try:
            restored = prior_interval if prior_interval is not None else "1s"
            opensearch_client.indices.put_settings(
                index=index_name, body={"index": {"refresh_interval": restored}}
            )
            opensearch_client.indices.refresh(index=index_name)
            logger.info(f"Restored refresh_interval={restored!r} on {index_name} and refreshed")
        except Exception as e:
            logger.warning(
                f"Failed to restore refresh_interval on {index_name}: {e}. "
                f'Run `PUT /{index_name}/_settings -d \'{{"index":{{"refresh_interval":"1s"}}}}\'`'
            )


class TranscriptIndexingService:
    """Handles chunking, embedding, and indexing transcripts into OpenSearch.

    Uses OpenSearch neural search for embedding generation. Embeddings are
    generated server-side via the neural ingest pipeline, which eliminates
    Python embedding overhead and enables hot-swap model changes.

    If neural search is not available, documents are indexed without embeddings
    and search falls back to BM25-only (keyword search).
    """

    def index_transcript_chunks(
        self,
        file_id: int,
        file_uuid: str,
        user_id: int,
        segments: list[dict[str, Any]],
        title: str,
        speakers: list[str],
        tags: list[str],
        upload_time: str | None = None,
        language: str = "en",
        content_type: str = "",
        duration: float | None = None,
        file_size: int | None = None,
        collection_ids: list[int] | None = None,
        accessible_user_ids: list[int] | None = None,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """Chunk and index a transcript.

        Embedding modes (in priority order):
        1. Neural pipeline available — OpenSearch generates embeddings server-side
        2. No embedding — BM25 keyword search only

        Args:
            file_id: Media file integer ID.
            file_uuid: Media file UUID.
            user_id: Owner user ID.
            segments: Transcript segments with start, end, text, speaker.
            title: File title.
            speakers: All speaker names in the file.
            tags: Tags associated with the file.
            upload_time: Upload time ISO string (defaults to now).
            language: Language code.
            content_type: MIME content type of the file.
            duration: Duration in seconds.
            file_size: File size in bytes.
            collection_ids: List of collection IDs the file belongs to.
            accessible_user_ids: List of user IDs with access to this file.
                Includes owner + users/groups with collection shares.
                If None, defaults to [user_id] (owner only).

        Returns:
            Dict of indexing stats. ``chunk_count`` 0 with a ``reason`` means there
            was legitimately nothing to index; a FAILURE raises (issue #495).
        """
        # These two return a dict with an explicit `reason`, not a bare 0 (issue #495).
        # Both are legitimate "nothing to index" outcomes — and while this method also
        # swallowed its exceptions into `return 0`, a real failure was *indistinguishable
        # from them*. That is what made a dead OpenSearch report a successful index.
        # Naming the reason is what lets zero-because-nothing-to-do and
        # zero-because-it-broke be told apart at all; the latter now raises.
        client = get_opensearch_client()
        if not client:
            logger.warning("OpenSearch client not initialized, skipping chunk indexing")
            return {"chunk_count": 0, "reason": "no_opensearch_client"}

        if not segments:
            logger.warning(f"No segments to index for file {file_uuid}")
            return {"chunk_count": 0, "reason": "no_segments"}

        ensure_chunks_index_exists()
        ensure_search_pipeline_exists()

        if upload_time is None:
            upload_time = datetime.datetime.now(datetime.UTC).isoformat()

        # 1. Chunk segments
        t_chunk_start = time.time()
        chunks = chunk_transcript_by_speaker_turns(
            segments=segments,
            file_uuid=file_uuid,
            file_id=file_id,
            user_id=user_id,
            title=title,
            speakers=speakers,
            tags=tags,
            upload_time=upload_time,
            language=language,
            content_type=content_type,
            duration=duration,
            file_size=file_size,
            collection_ids=collection_ids,
            organization_id=organization_id,
        )
        chunk_ms = round((time.time() - t_chunk_start) * 1000)

        if not chunks:
            # A transcript that now yields NO chunks still has to lose the ones it
            # used to have. `reindex_transcript` deletes first and is safe, but it
            # is not the primary path — `tasks/search_indexing_task` calls this
            # method directly, and the segments-exist-but-chunk-to-nothing case
            # (every segment empty after cleanup) reaches here rather than the
            # `not segments` guard above. Without this, those chunks stayed
            # searchable forever with no way to notice.
            pruned = self._prune_stale_chunks(file_uuid, keep_count=0)
            logger.warning(
                f"No chunks generated for file {file_uuid}; pruned {pruned} stale chunk(s)"
            )
            return {"chunk_count": 0, "reason": "no_chunks_generated", "stale_removed": pruned}

        # 2. Add indexed_at timestamp, accessible_user_ids, and the v6 fields
        now = datetime.datetime.now(datetime.UTC).isoformat()
        effective_user_ids = accessible_user_ids if accessible_user_ids else [user_id]
        header_roster = sorted(speakers or [])
        for chunk in chunks:
            chunk["indexed_at"] = now
            chunk["accessible_user_ids"] = effective_user_ids
            chunk[digest_mapping.DOC_TYPE_FIELD] = digest_mapping.DOC_TYPE_CHUNK
            # The pipeline embeds this field, not `content` — see
            # `_build_neural_ingest_pipeline`. BM25 still scores `content`.
            chunk["embedding_text"] = digest_mapping.build_embedding_text(
                title=title,
                recorded_at=upload_time,
                roster=header_roster,
                body=str(chunk.get("content") or ""),
            )

        # 3. Choose embedding mode and index
        t_index_start = time.time()
        try:
            use_neural = is_neural_pipeline_available()
            # The model that will actually produce these vectors, not the mode
            # they were produced in (#437). ``is_neural_pipeline_available`` is
            # what verifies the pipeline, so the label is resolved by the time we
            # read it; when it could not be named this is the same ``"neural"``
            # every pre-#437 document carries, so unknown stays one bucket.
            provenance = active_embedding_model()
            if use_neural:
                for chunk in chunks:
                    chunk["embedding_model"] = provenance
                logger.debug(f"Using neural ingest pipeline for file {file_uuid} ({provenance})")
            else:
                for chunk in chunks:
                    chunk["embedding_model"] = None
                logger.warning(f"Neural pipeline not available for {file_uuid}, text-only")

            # For very large transcripts (6h+ recordings produce 500+ chunks)
            # suspend index refresh during the bulk load so we don't pay the
            # per-batch refresh cost. The context manager restores the prior
            # refresh_interval on exit.
            with _suspended_refresh_for_large_index(
                settings.OPENSEARCH_CHUNKS_INDEX,
                chunk_count=len(chunks),
                threshold=settings.SEARCH_LARGE_TRANSCRIPT_CHUNKS,
            ):
                indexed = self._bulk_index_chunks(chunks, use_neural_pipeline=use_neural)

            # A PARTIAL index must fail the task (issue #495). `_bulk_index_chunks`
            # returns how many documents actually landed, and until this check existed
            # nothing compared it to how many were built: `_retry_failed_docs` gives up
            # after 2 attempts, logs "N documents failed after 2 retries", and returns —
            # and this method then reported `status: success` with `chunk_count` quietly
            # short. The file is left permanently half-searchable, with the missing
            # chunks unreachable by search and by RAG chat, and the only trace is one
            # ERROR line in a worker log nobody is reading.
            #
            # Raising is the honest outcome and is safe to retry: document ids are
            # deterministic (`{file_uuid}_{chunk_index}`), so a re-run overwrites rather
            # than duplicates. It is also strictly better than the alternative of
            # continuing — a task that reports success is never retried by anything.
            if indexed < len(chunks):
                raise RuntimeError(
                    f"Partial chunk index for file {file_uuid}: {indexed} of "
                    f"{len(chunks)} documents landed after retries. The file would "
                    "otherwise be reported as indexed while part of the transcript is "
                    "unreachable by search and RAG chat."
                )

            # Doc ids are deterministic (``{file_uuid}_{chunk_index}``), so the bulk
            # load above OVERWRITES chunks 0..len(chunks)-1 but cannot touch a longer
            # previous chunking's tail. Without this, a re-index after a segment merge,
            # a speaker-turn change or a SEARCH_CHUNK_TARGET_WORDS bump leaves stale
            # documents — old text, old speakers, old timestamps — that keep surfacing
            # in search and in RAG chat retrieval (issue #400).
            stale_removed = self._prune_stale_chunks(file_uuid, keep_count=len(chunks))

            # The digest plane (#403 Stage 3, addendum G1). It rides the chunk
            # index rather than the coordinator because `delete_transcript_chunks`
            # is an unqualified per-file delete: every rebuild trigger — version
            # bump, model switch, maintenance repair, manual reindex — comes
            # through here, and any one of them that regenerated chunks and not
            # digests would destroy the digest tier permanently.
            digest_count = self._index_digest_plane(
                file_id=file_id,
                file_uuid=file_uuid,
                base_metadata={
                    "user_id": user_id,
                    "title": title,
                    "tags": tags or [],
                    "upload_time": upload_time,
                    "language": language,
                    "content_type": content_type,
                    "duration": duration,
                    "file_size": file_size,
                    "collection_ids": collection_ids or [],
                    "accessible_user_ids": effective_user_ids,
                    "indexed_at": now,
                    "embedding_model": provenance if use_neural else None,
                    **({} if organization_id is None else {"organization_id": organization_id}),
                },
                use_neural=use_neural,
            )

            index_ms = round((time.time() - t_index_start) * 1000)
            total_ms = chunk_ms + index_ms
            mode_str = "neural" if use_neural else "text-only"
            logger.info(
                f"Indexed {indexed} chunks for file {file_uuid} "
                f"(mode: {mode_str}, chunk={chunk_ms}ms, index={index_ms}ms)"
            )
            # New/changed transcript content: chat must not keep serving
            # retrieval results computed against the previous version.
            _invalidate_chat_retrieval_cache()
            return {
                "chunk_count": indexed,
                "chunk_ms": chunk_ms,
                "index_ms": index_ms,
                "total_ms": total_ms,
                "mode": mode_str,
                "neural": use_neural,
                "stale_removed": stale_removed,
                "digest_sections": digest_count,
            }
        except Exception:
            # DO NOT swallow this into `return 0` (issue #495). It used to, and the
            # consequence was not a degraded result — it was a FALSE one. The caller,
            # `tasks/search_indexing_task`, has an int arm that wraps a bare count as
            # `{"chunk_count": result}`, marks the DB task **completed**, and returns
            # `{"status": "success", ...}`. So every failure in this method — a dead
            # OpenSearch, a mapping rejection, a partial bulk load — was reported to the
            # user, the task table and the notification as a successful index of zero
            # chunks. The task's own `except` (search_indexing_task.py, "Search indexing
            # failed for file …") could never fire, because nothing ever reached it.
            #
            # `logger.error` + a sentinel return is a reasonable pattern where the caller
            # inspects the sentinel. Here nothing did, and the sentinel was
            # indistinguishable from the legitimate zero-chunk case.
            logger.exception(f"Bulk indexing failed for file {file_uuid}")
            raise

    def delete_transcript_chunks(self, file_uuid: str) -> int:
        """Delete **every** indexed document for a file — both planes.

        Its two callers are file deletion and the full rebuild in
        :meth:`reindex_transcript`, and both mean "leave nothing behind":
        a digest that outlives its file is a readable summary of a deleted
        recording, and a digest that survives a rebuild whose transcript
        re-sectioned to fewer parts is a stale orphan (#400's failure, one
        plane over). Hence :func:`file_plane_query`, not
        :func:`chunk_plane_query`.

        Args:
            file_uuid: UUID of the file to delete documents for.

        Returns:
            Number of documents deleted.
        """
        if not opensearch_client:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        try:
            if not opensearch_client.indices.exists(index=index_name):
                return 0

            response = opensearch_client.delete_by_query(
                index=index_name,
                body={"query": file_plane_query(file_uuid)},
                refresh=True,
            )
            deleted: int = response.get("deleted", 0)
            logger.info(f"Deleted {deleted} chunks for file {file_uuid}")
            if deleted:
                _invalidate_chat_retrieval_cache()
            return deleted
        except Exception as e:
            logger.error(f"Error deleting chunks for file {file_uuid}: {e}")
            return 0

    def count_file_documents(self, file_uuid: str) -> int:
        """Count this file's still-indexed documents — **every plane**.

        The verification half of :meth:`delete_transcript_chunks`, and it lives
        here rather than at the caller so the two share one predicate: a survivor
        count built from a *different* predicate than the delete it checks is not
        a check. ``delete_transcript_chunks`` returns 0 for "no chunks", "index
        absent" and "the delete failed" alike, so this count — never that return
        value — is what proves an erasure complete.

        **Both planes, deliberately** (index v6, #403 Stage 3). A
        :func:`chunk_plane_query` count would report a clean sweep while the
        file's ``doc_type: digest`` sections were still indexed, and those
        sections are verbatim transcript text: the erasure would audit as
        complete with the recording's own words still searchable and still
        retrievable by chat. That is the failure the unqualified delete exists to
        prevent, arriving one step later through its verifier.

        Unlike everything else in this class it **raises** rather than returning
        0 when it cannot ask. Its caller is a GDPR Art. 17 erasure that records
        "could not verify" as a residual, and a 0 meaning "the cluster was
        unreachable" is precisely the wrong answer there. An **absent index** is
        still 0 — that is the cluster answering.

        The explicit refresh is the #435 lesson applied to the delete path: a
        ``count`` is a search and sees only what the last refresh made visible,
        so without it a delete that raised part-way could leave documents that
        the count cannot see and reports as gone. One refresh per erased file is
        not on any hot path.

        Args:
            file_uuid: UUID of the media file.

        Returns:
            Number of documents of any plane still matching the file.

        Raises:
            Exception: The cluster could not be reached or could not answer.
        """
        from opensearchpy.exceptions import NotFoundError

        if not opensearch_client:
            raise RuntimeError("OpenSearch client unavailable")

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        try:
            if not opensearch_client.indices.exists(index=index_name):
                return 0
            opensearch_client.indices.refresh(index=index_name)
            response = opensearch_client.count(
                index=index_name, body={"query": file_plane_query(file_uuid)}
            )
            return int(response["count"])
        except NotFoundError:
            return 0

    def _orphaned_document_ids(
        self,
        *,
        index_name: str,
        document_id: Callable[[int], str],
        first_orphan: int,
        plane_query: dict[str, Any],
        counter_field: str,
    ) -> list[str]:
        """Ids of this file's documents from ``first_orphan`` upward, read REALTIME.

        ``mget`` addresses documents **by id**, and an id lookup reads the translog:
        it sees a document the instant the bulk load returns, with no refresh. A
        ``count``/``search`` sees only what the last refresh made visible. That
        difference is the whole of the #435 fix — see :meth:`_prune_stale_chunks`.

        Measured on this stack: 5 documents bulk-loaded with ``refresh=False`` were
        all returned by ``mget`` while a ``count`` over the same predicate returned 0.

        Args:
            index_name: Index to probe.
            document_id: Builds the document id for an index — ``{uuid}_{n}`` for a
                chunk, ``{uuid}_digest_{n}`` for a digest section.
            first_orphan: First index that the new generation did not write.

        Returns:
            The ids that exist, in probe order. Empty means nothing to prune.
        """
        if not opensearch_client:
            return []

        found: list[str] = []
        ceiling: int | None = None
        start = first_orphan
        while True:
            probe = [document_id(n) for n in range(start, start + _ORPHAN_PROBE_WINDOW)]
            response = opensearch_client.mget(index=index_name, body={"ids": probe}, _source=False)
            window = [doc["_id"] for doc in response.get("docs", []) if doc.get("found")]
            if window:
                found.extend(window)
                start += _ORPHAN_PROBE_WINDOW
                continue

            # An empty window does NOT mean the tail ended (#400 follow-up). A
            # partially failed bulk load leaves a HOLE — `_extract_failed_docs`
            # drops permanently-failed documents — and returning here stranded
            # everything above a hole of `_ORPHAN_PROBE_WINDOW` or more,
            # permanently. That is exactly the failure #435 exists to prevent,
            # reintroduced by the walk's own stop condition.
            #
            # The searcher answers "is there anything higher?" for documents old
            # enough to be refreshed, which is precisely the case a hole implies
            # (the survivors above it are from the previous generation). The
            # unrefreshed case #435 cares about is contiguous from `first_orphan`,
            # so the FIRST window already found it and we never reach here.
            if ceiling is None:
                ceiling = self._probe_ceiling(
                    index_name, plane_query=plane_query, counter_field=counter_field
                )
            if ceiling is None or start > ceiling:
                return found
            start += _ORPHAN_PROBE_WINDOW

    def _probe_ceiling(
        self, index_name: str, *, plane_query: dict[str, Any], counter_field: str
    ) -> int | None:
        """Highest value of *counter_field* the SEARCHER can see for this plane.

        Deliberately a search, and deliberately only consulted after an empty
        probe window: a searcher cannot see the unrefreshed writes the id probe
        exists for, but it is the only thing that can say how far above a hole to
        keep looking. Paying for it lazily keeps it off the common path — a
        shrinking re-chunk finds its tail in the first window and never gets here,
        and a growing or unchanged one pays one small aggregation.

        *plane_query* is the caller's own plane predicate — ``chunk_plane_query``
        or ``digest_plane_query`` for the whole file — so the ceiling is measured
        over exactly the documents the delete will target, compat arm included.
        *counter_field* is the field the document id counts up with:
        ``chunk_index`` for the chunk plane, ``digest_section`` for the digest
        plane (whose ``chunk_index`` is a NEGATIVE sentinel and counts the wrong
        way).

        Returns:
            The maximum value, or ``None`` when the searcher knows nothing (a
            fresh or wholly-unrefreshed generation) or the query failed. ``None``
            means "stop walking" — the behaviour the walk already had, so this can
            only ever find more, never less.
        """
        try:
            response = opensearch_client.search(
                index=index_name,
                body={
                    "size": 0,
                    "query": plane_query,
                    "aggs": {"ceiling": {"max": {"field": counter_field}}},
                },
            )
        except Exception as exc:  # noqa: BLE001 — the walk degrades, it must not fail
            logger.debug(f"Could not read the orphan ceiling: {exc}")
            return None

        value = (response.get("aggregations") or {}).get("ceiling", {}).get("value")
        return None if value is None else int(value)

    def _prune_stale_chunks(self, file_uuid: str, *, keep_count: int) -> int:
        """Delete chunks left behind by a longer previous chunking of this file.

        Called after every bulk load. Chunks ``0..keep_count-1`` were just
        overwritten in place (the doc id is derived from ``chunk_index``); anything
        at or above ``keep_count`` belongs to a chunking that no longer exists.

        **The gate is an id probe, not a count, and that is issue #435.** A ``count``
        is a search, and a search sees only what the last ``refresh`` made visible,
        while the bulk load above uses ``refresh=False``. So a second index of the
        same file inside the refresh window used to find an empty tail, skip the
        delete, and leave the orphans **permanently** — nothing later re-examined
        them and a subsequent refresh triggered no prune.

        The previous version of this docstring called that unreachable ("no pipeline
        path re-indexes the same file twice inside one second"). Both halves of that
        were false, and both were measured rather than argued:

        * **Reachable, and not narrowly.** A back-to-back pair of
          ``index_transcript_chunks`` calls completes in 125–224 ms at this cluster's
          production ``refresh_interval``, and the stale tail survived **11 of 12
          pairs**.
        * **Nothing serialises the callers.** Five dispatch sites reach the
          single-file indexing task — ``tasks/transcription/postprocess.py``,
          ``tasks/transcription/background.py``, ``services/task_recovery_service.py``,
          ``api/endpoints/files/reprocess.py`` (user-triggered, at any moment, with no
          in-flight or lock check) and ``scripts/corpus_injection/injector.py`` — plus
          the batch loop in ``tasks/reindex_task.py``. The recovery sweep is the
          sharpest: it branches on the index record being *missing*, not on the
          original task having *stopped*, so a file whose indexing is in flight gets a
          second dispatch by design.

        **What the mechanism now guarantees**, and only this: any orphan that exists
        when :meth:`_orphaned_document_ids` runs is found, whether or not a refresh
        has made it searchable. The probe is realtime; the ``refresh`` below is then
        required because ``delete_by_query`` executes as a **scroll search** and
        therefore cannot see what the probe just found — measured directly, a
        ``delete_by_query`` over 4 unrefreshed documents deletes 0. That is also why
        a ``_seq_no`` or ``_version`` predicate cannot replace the gate: a predicate
        changes which *visible* documents match, it cannot make invisible ones
        visible.

        **What it does NOT guarantee.** Two genuinely overlapping
        ``index_transcript_chunks`` calls for one file are still not serialised by
        anything. If a second call's probe runs before a first call's bulk load
        returns, the tail it has not written yet cannot be found — by any mechanism
        short of a lock. Interleaved writers on one file remain a coordination
        problem, not a visibility one.

        **Cost, measured rather than assumed** (real mapping, 384-d HNSW, 50
        chunks/file, 25 samples, at 5k/15k/30k documents): the probe is
        **4.0–4.8 ms** against the **3.7–4.1 ms** count it replaces. The alternative
        — refreshing unconditionally before the gate — is **95 ms median /
        116–184 ms mean per file**, near-flat in index size because it is dominated
        by building the HNSW graph for the vectors just written, which is **+41 s to
        +79 s on a 432-file reindex that takes 182 s**. Hence the refresh sits behind
        the gate, on the path where a re-chunk actually shrank, and a full reindex
        pays none of it.

        A failure here is logged, not raised: the new chunks are already indexed and
        correct, and the caller must not report a successful index as a failure.

        Args:
            file_uuid: UUID of the media file just indexed.
            keep_count: Number of chunks the new chunking produced.

        Returns:
            Number of stale chunks deleted.
        """
        if not opensearch_client:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        try:
            stale_ids = self._orphaned_document_ids(
                index_name=index_name,
                document_id=lambda n: f"{file_uuid}_{n}",
                first_orphan=keep_count,
                plane_query=chunk_plane_query(file_uuid),
                counter_field="chunk_index",
            )
            if not stale_ids:
                return 0

            # The probe read the translog; delete_by_query reads a searcher. Without
            # this the delete matches nothing it was just told about.
            opensearch_client.indices.refresh(index=index_name)
            response = opensearch_client.delete_by_query(
                index=index_name,
                body={"query": chunk_plane_query(file_uuid, from_chunk_index=keep_count)},
                refresh=True,
                conflicts="proceed",
            )
            deleted = int(response.get("deleted", 0))
            conflicts = int(response.get("version_conflicts", 0) or 0)
            logger.info(
                f"Pruned {deleted} stale chunk(s) for file {file_uuid} "
                f"(re-chunk shrank to {keep_count} chunks)"
            )
            # `deleted != len(stale_ids)` has THREE causes and this used to
            # attribute all of them to the first, so a real conflict read as a
            # predicate bug:
            #   fewer  — the predicate declined documents the probe found. The known
            #            cause is a lost pre-v6 compat arm (chunks written before
            #            index v6 carry no `doc_type`), or documents skipped by
            #            `conflicts="proceed"` — which `conflicts` distinguishes.
            #   more   — the range delete removed documents the probe did not
            #            enumerate. Normal and healthy: the probe is a boolean gate
            #            over a windowed id walk, the delete is a range.
            if conflicts:
                logger.warning(
                    f"Stale-chunk prune for file {file_uuid} skipped {conflicts} document(s) "
                    "on version conflict; they keep their stale content until the next prune"
                )
            elif deleted < len(stale_ids):
                logger.warning(
                    f"Stale-chunk prune for file {file_uuid} found {len(stale_ids)} "
                    f"orphan id(s) but the chunk-plane predicate deleted only {deleted} — "
                    "the predicate is narrower than the id probe"
                )
            return deleted
        except Exception as e:
            logger.error(
                f"Could not prune stale chunks for file {file_uuid} "
                f"(chunks >= {keep_count} may still be searchable): {e}"
            )
            return 0

    def _index_digest_plane(
        self,
        *,
        file_id: int,
        file_uuid: str,
        base_metadata: dict[str, Any],
        use_neural: bool,
    ) -> int:
        """Regenerate and index this file's digest documents.

        Regeneration happens **here**, on the per-file index path, for the
        reason addendum G1 gives: `delete_transcript_chunks` is unqualified and
        every rebuild trigger routes through this method's caller, so anything
        that is not regenerated here is destroyed. Stage 2's
        ``source_fingerprint`` short-circuit is what makes that affordable — an
        unchanged transcript costs a SHA-256, not a TextRank — and because the
        fingerprint covers the *resolved* speaker display name, a rename
        invalidates the row by itself and needs no separate trigger (#405).

        Its own session: the callers are a Celery task that has one open over a
        batch of files and an API path that has none, and borrowing the former's
        would make a digest failure roll back a whole batch's progress.

        An empty digest is a valid outcome (a ten-second clip has no sentence
        long enough), and a failure here is logged rather than raised: the
        chunks are already indexed and correct, and a caller must not report a
        good index as a failure.

        Args:
            file_id: Media file integer id.
            file_uuid: Media file UUID.
            base_metadata: Per-file fields shared with chunk documents. The ACL
                rewrite keys on ``file_id`` and the tenant backfill on
                ``file_uuid`` (addendum G5), so :func:`build_digest_documents`
                puts **both** on every digest — do not strip either.
            use_neural: Whether to run the documents through the ingest pipeline.

        Returns:
            Number of digest sections indexed.
        """
        try:
            from app.db.session_utils import session_scope
            from app.services.ingest_artifacts import generate_file_artifacts
            from app.services.ingest_artifacts import resolve_recorded_date_for_file

            with session_scope() as db:
                # **This is the back-catalogue backfill**, and it is here rather than in a
                # one-off script because it needs no re-ingest: the filename and the
                # transcript are already in Postgres for every existing row, so the first
                # reindex after this ships dates the whole library. Unlike the artifacts
                # below it has no fingerprint short-circuit, so an unchanged file is still
                # resolved — which is the entire point for rows that predate the column.
                #
                # Its own try: a date is an enrichment and the chunks are already indexed
                # and correct. A regex failure here must not cost the digest plane, and
                # the outer handler would report the whole digest index as failed.
                try:
                    resolve_recorded_date_for_file(db, file_id)
                except Exception as date_exc:  # noqa: BLE001 — enrichment, never fatal
                    logger.warning(
                        "Could not resolve recorded_date for file %s: %s", file_id, date_exc
                    )

                row = generate_file_artifacts(db, file_id)
                if row is None:
                    return 0
                digest = dict(row.digest or {})
                facts = dict(row.facts or {})

            documents = digest_mapping.build_digest_documents(
                file_uuid=file_uuid,
                file_id=file_id,
                digest=digest,
                facts=facts,
                base_metadata=base_metadata,
            )
            ids = digest_mapping.digest_document_ids(file_uuid, digest)
            written = 0
            if documents:
                written = self._bulk_index_documents(
                    list(zip(ids, documents, strict=True)), use_neural
                )
            # Same orphan hazard as #400's chunk tail: the ids embed the section
            # number, so a digest that re-sections shorter leaves the extras
            # behind, still matching every query the file matches.
            self._prune_stale_digests(file_uuid, keep_count=len(documents))

            # Report what LANDED, not what was built (issue #495). This used to
            # discard `_bulk_index_documents`'s return value entirely and return
            # `len(documents)`, so `digest_sections` in the task result was the number
            # of sections *generated* — reported identically whether all of them were
            # written or none were. `_bulk_index_documents` logs the failures and
            # returns a short count; nothing read it.
            #
            # Unlike the chunk plane above this does NOT raise, and the asymmetry is
            # deliberate: the `except` below already declines to fail the caller for a
            # digest problem, because the digest tier is derived enrichment while the
            # chunks are the transcript itself. A missing digest degrades summarization;
            # missing chunks make part of a recording unfindable. Reporting the true
            # count is what that decision needs to stay honest — "we chose not to fail"
            # is defensible, "we reported a number we did not verify" is not.
            if written < len(documents):
                logger.error(
                    f"Digest plane for file {file_uuid} is incomplete: {written} of "
                    f"{len(documents)} sections landed"
                )
            return written
        except Exception as exc:  # noqa: BLE001 — chunks are indexed; do not fail the caller
            logger.error(f"Could not index digest plane for file {file_uuid}: {exc}")
            return 0

    def _prune_stale_digests(self, file_uuid: str, *, keep_count: int) -> int:
        """Delete digest sections left behind by a longer previous sectioning.

        The same realtime gate as :meth:`_prune_stale_chunks`, for the same reason
        (#435) and with the same guarantee: this plane is written by the same
        unrefreshed bulk load, from the same five unserialised dispatch paths, so a
        count gate here misses an orphan section exactly as often. Fixing one plane
        and describing the race as closed would be a docstring asserting a guarantee
        the code does not provide — which is the defect #435 is about.
        """
        if not opensearch_client:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        try:
            stale_ids = self._orphaned_document_ids(
                index_name=index_name,
                document_id=lambda n: digest_mapping.digest_document_id(file_uuid, n),
                first_orphan=keep_count,
                plane_query=digest_plane_query(file_uuid),
                # NOT `chunk_index`: a digest's is a negative sentinel that counts
                # the wrong way. `digest_section` is what its document id counts up.
                counter_field="digest_section",
            )
            if not stale_ids:
                return 0

            opensearch_client.indices.refresh(index=index_name)
            response = opensearch_client.delete_by_query(
                index=index_name,
                body={"query": digest_plane_query(file_uuid, from_section=keep_count)},
                refresh=True,
                conflicts="proceed",
            )
            deleted = int(response.get("deleted", 0))
            logger.info(f"Pruned {deleted} stale digest section(s) for file {file_uuid}")
            if deleted != len(stale_ids):
                logger.warning(
                    f"Stale-digest prune for file {file_uuid} found {len(stale_ids)} "
                    f"orphan id(s) but the digest-plane predicate deleted {deleted}"
                )
            return deleted
        except Exception as e:  # noqa: BLE001
            logger.error(f"Could not prune stale digest sections for file {file_uuid}: {e}")
            return 0

    def _bulk_index_documents(
        self, documents: list[tuple[str, dict[str, Any]]], use_neural_pipeline: bool
    ) -> int:
        """Bulk index ``(_id, document)`` pairs. Used by the digest plane.

        Separate from :meth:`_bulk_index_chunks` because that one derives the id
        from ``chunk_index``, and a digest's id is ``{uuid}_digest_{n}`` —
        ``{uuid}_0`` is already chunk 0.
        """
        if not opensearch_client or not documents:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        bulk_body: list[Any] = []
        for doc_id, document in documents:
            action: dict[str, Any] = {"index": {"_index": index_name, "_id": doc_id}}
            if use_neural_pipeline:
                action["index"]["pipeline"] = settings.OPENSEARCH_NEURAL_PIPELINE
            bulk_body.append(action)
            bulk_body.append(document)

        response = opensearch_client.bulk(body=bulk_body, refresh=False)
        if response.get("errors"):
            failed = [
                item["index"]
                for item in response.get("items", [])
                if item.get("index", {}).get("error")
            ]
            logger.error(f"Digest bulk index reported {len(failed)} failure(s): {failed[:3]}")
            return len(documents) - len(failed)
        return len(documents)

    def reindex_transcript(
        self,
        file_id: int,
        file_uuid: str,
        user_id: int,
        segments: list[dict[str, Any]],
        title: str,
        speakers: list[str],
        tags: list[str],
        upload_time: str | None = None,
        language: str = "en",
        content_type: str = "",
        duration: float | None = None,
        file_size: int | None = None,
        collection_ids: list[int] | None = None,
        accessible_user_ids: list[int] | None = None,
        organization_id: int | None = None,
    ) -> int:
        """Re-chunk and re-index a single transcript.

        Args:
            Same as index_transcript_chunks.

        Returns:
            Number of chunks indexed.
        """
        # Delete every plane first. Not redundant with the tail prunes
        # ``index_transcript_chunks`` does (issue #400): a full rebuild wants a
        # clean slate, and this clears **every** plane rather than the chunk tail —
        # notably the digest plane, which is regenerated by the same call
        # (addendum G1).
        #
        # This used to claim it was "the only path that also clears the documents
        # of a transcript that now yields NO chunks", which was true and mattered,
        # because `reindex_transcript` is NOT the primary path —
        # `tasks/search_indexing_task` calls `index_transcript_chunks` directly.
        # That method now prunes on its own zero-chunk branch, so the claim is
        # obsolete rather than merely narrow.
        self.delete_transcript_chunks(file_uuid)

        # Re-index
        result = self.index_transcript_chunks(
            file_id=file_id,
            file_uuid=file_uuid,
            user_id=user_id,
            segments=segments,
            title=title,
            speakers=speakers,
            tags=tags,
            upload_time=upload_time,
            language=language,
            content_type=content_type,
            duration=duration,
            file_size=file_size,
            collection_ids=collection_ids,
            accessible_user_ids=accessible_user_ids,
            organization_id=organization_id,
        )
        chunk_count: int = result.get("chunk_count", 0)
        return chunk_count

    def _bulk_index_chunks(
        self, chunks: list[dict[str, Any]], use_neural_pipeline: bool = False
    ) -> int:
        """Bulk index chunks to OpenSearch in batches.

        Splits chunks into batches of SEARCH_BULK_BATCH_SIZE to avoid
        timeouts on large files. Failed documents with transient errors
        are retried with exponential backoff.

        Args:
            chunks: List of chunk documents to index.
            use_neural_pipeline: If True, use neural ingest pipeline for embedding.

        Returns:
            Number of successfully indexed chunks.
        """
        if not opensearch_client:
            raise RuntimeError("OpenSearch client not initialized")

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        batch_size = settings.SEARCH_BULK_BATCH_SIZE
        total_indexed = 0

        for batch_start in range(0, len(chunks), batch_size):
            batch = chunks[batch_start : batch_start + batch_size]
            bulk_body: list[Any] = []

            for chunk in batch:
                doc_id = f"{chunk['file_uuid']}_{chunk['chunk_index']}"
                index_action: dict[str, Any] = {
                    "index": {
                        "_index": index_name,
                        "_id": doc_id,
                    }
                }

                # Use neural ingest pipeline if enabled
                if use_neural_pipeline:
                    index_action["index"]["pipeline"] = settings.OPENSEARCH_NEURAL_PIPELINE

                bulk_body.append(index_action)
                bulk_body.append(chunk)

            response = opensearch_client.bulk(body=bulk_body, refresh=False)

            if response.get("errors"):
                failed_docs = self._extract_failed_docs(response, batch)
                succeeded = len(batch) - len(failed_docs)
                total_indexed += succeeded

                if failed_docs:
                    retried = self._retry_failed_docs(failed_docs, index_name, use_neural_pipeline)
                    total_indexed += retried
            else:
                total_indexed += len(batch)

            if len(chunks) > batch_size:
                logger.debug(
                    f"Bulk batch {batch_start // batch_size + 1}: "
                    f"indexed {len(batch)} chunks (offset {batch_start})"
                )

        return total_indexed

    def _extract_failed_docs(
        self, response: dict[str, Any], batch: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Extract documents that failed with transient errors from a bulk response.

        Permanent errors (e.g. mapping exceptions) are logged and skipped.
        Transient errors (e.g. circuit breaker, rejected execution) are returned
        for retry.

        Args:
            response: OpenSearch bulk response.
            batch: The original batch of chunk documents.

        Returns:
            List of chunk documents that should be retried.
        """
        failed_docs: list[dict[str, Any]] = []
        permanent_count = 0

        for i, item in enumerate(response.get("items", [])):
            index_result = item.get("index", {})
            error_info = index_result.get("error")
            if not error_info:
                continue

            error_type = error_info.get("type", "")
            error_reason = error_info.get("reason", "")

            if error_type in _PERMANENT_ERROR_TYPES:
                # Permanent error -- log and skip
                permanent_count += 1
                logger.error(f"Permanent bulk index error (skipping): {error_type}: {error_reason}")
            elif error_type in _RETRYABLE_ERROR_TYPES:
                # Known transient error -- eligible for retry
                if i < len(batch):
                    failed_docs.append(batch[i])
                logger.warning(
                    f"Transient bulk index error (will retry): {error_type}: {error_reason}"
                )
            else:
                # Unknown error type -- log and skip (don't blindly retry)
                permanent_count += 1
                logger.error(f"Unknown bulk index error (skipping): {error_type}: {error_reason}")

        if permanent_count:
            logger.error(f"Bulk indexing had {permanent_count} permanent errors (not retried)")
        if failed_docs:
            logger.info(f"Bulk indexing has {len(failed_docs)} transient failures to retry")

        return failed_docs

    def _retry_failed_docs(
        self,
        failed_docs: list[dict[str, Any]],
        index_name: str,
        use_neural: bool,
        max_retries: int = _BULK_RETRY_ATTEMPTS,
    ) -> int:
        """Retry failed documents with exponential backoff.

        Args:
            failed_docs: List of chunk documents to retry.
            index_name: OpenSearch index name.
            use_neural: Whether to use the neural ingest pipeline.
            max_retries: Maximum number of retry attempts.

        Returns:
            Number of successfully indexed documents after retries.
        """
        if not opensearch_client or not failed_docs:
            return 0

        retried_count = 0
        remaining = list(failed_docs)

        for attempt in range(1, max_retries + 1):
            if not remaining:
                break

            # Exponential with jitter, capped: ~1s, 2s, 4s, 8s (+/- 25%).
            # `secrets` rather than `random` because ruff's S311 is right that the
            # stdlib PRNG is the wrong default, and the jitter only needs to
            # decorrelate concurrent workers — which a CSPRNG does equally well, at a
            # cost measured in microseconds, at most 4 times per failed batch. That is
            # cheaper than justifying a suppression.
            backoff = min(_BULK_RETRY_BASE_SECONDS * (2 ** (attempt - 1)), _BULK_RETRY_MAX_SECONDS)
            backoff *= 0.75 + (secrets.randbelow(1000) / 2000.0)
            logger.info(
                f"Retrying {len(remaining)} failed docs (attempt {attempt}/{max_retries}, "
                f"backoff {backoff:.1f}s)"
            )
            time.sleep(backoff)

            bulk_body: list[Any] = []
            for chunk in remaining:
                doc_id = f"{chunk['file_uuid']}_{chunk['chunk_index']}"
                index_action: dict[str, Any] = {
                    "index": {
                        "_index": index_name,
                        "_id": doc_id,
                    }
                }
                if use_neural:
                    index_action["index"]["pipeline"] = settings.OPENSEARCH_NEURAL_PIPELINE
                bulk_body.append(index_action)
                bulk_body.append(chunk)

            try:
                response = opensearch_client.bulk(body=bulk_body, refresh=False)
            except Exception as e:
                logger.error(f"Retry attempt {attempt} bulk call failed: {e}")
                continue

            if not response.get("errors"):
                retried_count += len(remaining)
                remaining = []
                break

            # Check which ones still failed
            still_failed: list[dict[str, Any]] = []
            for i, item in enumerate(response.get("items", [])):
                index_result = item.get("index", {})
                if index_result.get("error"):
                    if i < len(remaining):
                        still_failed.append(remaining[i])
                else:
                    retried_count += 1

            remaining = still_failed

        if remaining:
            logger.error(f"{len(remaining)} documents failed after {max_retries} retries")

        if retried_count:
            logger.info(f"Successfully retried {retried_count} documents")

        return retried_count
