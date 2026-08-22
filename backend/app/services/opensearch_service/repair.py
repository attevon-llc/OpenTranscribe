"""Detection and repair of corrupted OpenSearch indices."""

import datetime
import logging
from typing import Any

from app.core.config import settings
from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v4
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.aliases import get_speaker_embedding_dimension
from app.services.opensearch_service.aliases import invalidate_active_speaker_index_cache
from app.services.opensearch_service.client import _is_index_corruption_error
from app.services.opensearch_service.indices import ensure_indices_exist

logger = logging.getLogger(__name__)


def _verify_index_queryable(index_name: str) -> bool:
    """Confirm *index_name* answers the kind of query it exists to answer.

    A ``match_all`` is a BM25/scan query, and Lucene's text segments are
    crash-safe while its HNSW vector files are not. Verifying a vector-backed
    index with ``match_all`` therefore reports "repaired" for the exact failure
    mode issue #540 describes — BM25 fine, every kNN answering 503 — and the
    caller then never escalates to a strategy that would actually fix it.

    Args:
        index_name: Concrete index to verify.

    Returns:
        True if the index answered. For an index declaring a knn_vector
        dimension this means the **vector** plane answered; an index holding no
        documents counts as answering (see ``KnnProbeResult.is_serviceable``).

    Raises:
        Exception: Propagated from the probe query so the caller's ``except``
            still sees a failed strategy as failed.
    """
    if not _client.opensearch_client:
        return False

    if _client._supports_ann_search(index_name):
        return _client.probe_knn_health(index_name).is_serviceable

    _client.opensearch_client.search(index=index_name, body={"query": {"match_all": {}}, "size": 0})
    return True


def _repair_index(index_name: str, db: "Any | None" = None) -> bool:
    """Attempt to repair a corrupted OpenSearch index.

    Strategy 0: Detect wrong mapping type (e.g. embedding stored as float
    instead of knn_vector due to dynamic mapping). Requires delete + recreate.

    Then tries close/reopen (fixes stale file handles on HNSW vector segments),
    force merge, and finally full rebuild from PostgreSQL data.

    Args:
        index_name: Name of the index to repair.
        db: Optional SQLAlchemy session for DB-based rebuild.

    Returns:
        True if the index was successfully repaired.
    """
    if not _client.opensearch_client:
        return False

    # Strategy 0: Detect wrong mapping type on speaker index embedding field.
    # If the index was auto-created by OpenSearch with dynamic mapping, the
    # embedding field will be "float" instead of "knn_vector". Close/reopen
    # and force-merge cannot fix this — the index must be deleted and recreated.
    if index_name == get_speaker_index():
        try:
            mapping = _client.opensearch_client.indices.get_mapping(index=index_name)
            properties = mapping.get(index_name, {}).get("mappings", {}).get("properties", {})
            emb_type = properties.get("embedding", {}).get("type", "")
            if emb_type and emb_type != "knn_vector":
                logger.warning(
                    f"Speaker index '{index_name}' has embedding type '{emb_type}' "
                    f"instead of 'knn_vector'. Deleting and recreating with correct mapping."
                )
                doc_count = _client.opensearch_client.count(index=index_name).get("count", 0)
                _client.opensearch_client.indices.delete(index=index_name)
                logger.info(f"Deleted broken index {index_name} ({doc_count} docs)")
                # Recreate with correct mapping
                ensure_indices_exist()
                invalidate_active_speaker_index_cache()
                logger.info(f"Recreated index {index_name} with knn_vector mapping")
                return True
        except Exception as e:
            logger.warning(f"Wrong-mapping detection/fix failed for {index_name}: {e}")

    # Strategy 1: Close and reopen to force re-acquisition of file handles
    try:
        _client.opensearch_client.indices.close(index=index_name)
        _client.opensearch_client.indices.open(index=index_name)
        if _verify_index_queryable(index_name):
            logger.info(f"Index {index_name} repaired via close/reopen")
            _client.reset_knn_health_cache()
            return True
        logger.warning(f"Close/reopen left {index_name} still unable to answer a kNN query")
    except Exception as e:
        logger.warning(f"Close/reopen failed for {index_name}: {e}")

    # Strategy 2: Force merge to compact corrupted segments
    try:
        _client.opensearch_client.indices.forcemerge(index=index_name, max_num_segments=1)
        if _verify_index_queryable(index_name):
            logger.info(f"Index {index_name} repaired via force merge")
            _client.reset_knn_health_cache()
            return True
        logger.warning(f"Force merge left {index_name} still unable to answer a kNN query")
    except Exception as e:
        logger.warning(f"Force merge failed for {index_name}: {e}")

    # Strategy 3: For kNN indices, rebuild from PostgreSQL data (last resort)
    try:
        mapping = _client.opensearch_client.indices.get_mapping(index=index_name)
        properties = mapping.get(index_name, {}).get("mappings", {}).get("properties", {})
        has_knn = any(
            prop.get("type") == "knn_vector"
            for prop in properties.values()
            if isinstance(prop, dict)
        )
        if has_knn and index_name == get_speaker_index() and db is not None:
            logger.info(f"Attempting rebuild of kNN index {index_name} from PostgreSQL data...")
            result = rebuild_speaker_index(db)
            if result.get("status") == "rebuilt":
                logger.info(
                    f"Index {index_name} rebuilt from DB: {result.get('speakers_indexed', 0)} speakers"
                )
                return True
    except Exception as rebuild_err:
        logger.error(f"Rebuild from DB failed for {index_name}: {rebuild_err}")

    logger.error(f"All repair strategies failed for {index_name}")
    return False


def _drop_rebuild_index(rebuild_index: str) -> None:
    """Delete the temporary rebuild index. Only safe while ``speakers`` still exists.

    Args:
        rebuild_index: Name of the temporary index.
    """
    try:
        if _client.opensearch_client and _client.opensearch_client.indices.exists(
            index=rebuild_index
        ):
            _client.opensearch_client.indices.delete(index=rebuild_index)
    except Exception as cleanup_err:  # noqa: BLE001 - cleanup must not mask the real error
        logger.debug("Failed to clean up rebuild index: %s", cleanup_err)


def rebuild_speaker_index(db: "Any", allow_empty_rebuild: bool = False) -> dict[str, Any]:
    """Rebuild the speakers index from PostgreSQL + speakers_v4 data.

    Creates a temporary index, copies valid speaker data from the working
    speakers_v4 index, then swaps it in as the new speakers index. This is
    the nuclear option for when the speakers index has corrupted kNN segments
    that cannot be repaired via close/reopen or force-merge.

    **The corrupted index is the only other copy of those voiceprints**, and
    embeddings cannot be recomputed without the original media. So it is deleted
    only after the rebuild index has been counted and found to hold every
    document that was loaded into it, and never at all when there is nothing to
    restore — a missing ``speakers_v4`` used to mean "delete everything and
    create an empty index", turning an unreadable index into an erased one.
    Likewise the temporary index is retained (and named in the result) when the
    copy back is short, because at that point it holds the only complete copy.

    Args:
        db: SQLAlchemy Session for querying Speaker rows.
        allow_empty_rebuild: Delete and recreate the speakers index even when no
            embeddings could be recovered. Restores service at the cost of every
            voiceprint; an operator with a snapshot wants the default.

    Returns:
        Dict with rebuild status and count of speakers indexed. ``status`` is
        ``rebuilt``, ``refused`` (nothing was destroyed) or ``error``.
    """
    from sqlalchemy.orm import Session as SASession

    if not isinstance(db, SASession):
        return {"status": "error", "message": "Invalid database session", "speakers_indexed": 0}

    if not _client.opensearch_client:
        return {
            "status": "error",
            "message": "OpenSearch client not available",
            "speakers_indexed": 0,
        }

    from app.models.media import Speaker

    speaker_index = get_speaker_index()
    v4_index = f"{speaker_index}_v4"
    rebuild_index = f"{speaker_index}_rebuild"

    try:
        # Step 1: Query speakers with cluster_id from PostgreSQL
        speakers = db.query(Speaker).filter(Speaker.cluster_id.isnot(None)).all()
        logger.info(f"Rebuild: found {len(speakers)} speakers with cluster assignments in DB")

        # Step 2: Build a lookup of speaker_uuid -> Speaker from DB
        speaker_map = {str(s.uuid): s for s in speakers}

        # Step 3: Fetch embeddings from speakers_v4 index
        docs_to_index: list[dict[str, Any]] = []
        if _client.opensearch_client.indices.exists(index=v4_index):
            # Paginate through all speaker docs in v4 index
            search_after = None
            while True:
                query: dict[str, Any] = {
                    "size": 500,
                    "query": {
                        "bool": {
                            "must_not": [
                                {"exists": {"field": "document_type"}},
                            ],
                        }
                    },
                    "sort": [{"_id": "asc"}],
                    "_source": [
                        "speaker_uuid",
                        "speaker_id",
                        "user_id",
                        "name",
                        "display_name",
                        "profile_id",
                        "profile_uuid",
                        "collection_ids",
                        "media_file_id",
                        "segment_count",
                        "embedding",
                    ],
                }
                if search_after:
                    query["search_after"] = search_after

                response = _client.opensearch_client.search(index=v4_index, body=query)
                hits = response["hits"]["hits"]
                if not hits:
                    break

                for hit in hits:
                    source = hit["_source"]
                    speaker_uuid = source.get("speaker_uuid")
                    embedding = source.get("embedding")
                    if speaker_uuid and embedding and speaker_uuid in speaker_map:
                        docs_to_index.append(source)

                search_after = hits[-1]["sort"]

            logger.info(f"Rebuild: fetched {len(docs_to_index)} embeddings from {v4_index}")
        else:
            logger.warning(f"Rebuild: {v4_index} index does not exist, no embeddings to recover")

        # Step 4: Create temporary rebuild index with correct mapping
        if _client.opensearch_client.indices.exists(index=rebuild_index):
            _client.opensearch_client.indices.delete(index=rebuild_index)

        speaker_index_config = {
            "settings": {
                "index": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                    "knn": True,
                }
            },
            "mappings": {
                "properties": {
                    "speaker_id": {"type": "integer"},
                    "speaker_uuid": {"type": "keyword"},
                    "profile_id": {"type": "integer"},
                    "profile_uuid": {"type": "keyword"},
                    "user_id": {"type": "integer"},
                    "name": {"type": "keyword"},
                    "collection_ids": {"type": "integer"},
                    "media_file_id": {"type": "integer"},
                    "segment_count": {"type": "integer"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": get_speaker_embedding_dimension(),
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "lucene",
                            "parameters": {
                                "ef_construction": 128,
                                "m": 24,
                            },
                        },
                    },
                }
            },
        }

        _client.opensearch_client.indices.create(index=rebuild_index, body=speaker_index_config)
        logger.info(f"Rebuild: created temporary index {rebuild_index}")

        # Step 5: Bulk-index documents into rebuild index
        indexed_count = 0
        if docs_to_index:
            bulk_body: list[dict[str, Any]] = []
            for source in docs_to_index:
                bulk_body.append(
                    {
                        "index": {
                            "_index": rebuild_index,
                            "_id": str(source["speaker_uuid"]),
                        }
                    }
                )
                doc = {
                    "speaker_id": source.get("speaker_id"),
                    "speaker_uuid": str(source["speaker_uuid"]),
                    "profile_id": source.get("profile_id"),
                    "profile_uuid": str(source["profile_uuid"])
                    if source.get("profile_uuid")
                    else None,
                    "user_id": source.get("user_id"),
                    "name": source.get("name"),
                    "display_name": source.get("display_name"),
                    "collection_ids": source.get("collection_ids", []),
                    "media_file_id": source.get("media_file_id"),
                    "segment_count": source.get("segment_count", 1),
                    "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "embedding": source["embedding"],
                }
                bulk_body.append(doc)

            # Bulk index in batches of 500
            batch_size = 1000  # 500 action pairs
            for i in range(0, len(bulk_body), batch_size):
                batch = bulk_body[i : i + batch_size]
                resp = _client.opensearch_client.bulk(body=batch, refresh="wait_for")
                if not resp.get("errors"):
                    indexed_count += len(batch) // 2
                else:
                    # Count successes individually
                    for item in resp.get("items", []):
                        if item.get("index", {}).get("status") in (200, 201):
                            indexed_count += 1

        logger.info(f"Rebuild: indexed {indexed_count} speakers into {rebuild_index}")

        # Step 5b: verify the rebuild index BEFORE the corrupted one is deleted.
        try:
            _client.opensearch_client.indices.refresh(index=rebuild_index)
            rebuild_count = int(_client.opensearch_client.count(index=rebuild_index)["count"])
        except Exception as count_err:
            _drop_rebuild_index(rebuild_index)
            logger.error(f"Rebuild: could not verify {rebuild_index}: {count_err}")
            return {
                "status": "error",
                "message": f"Rebuild index could not be verified: {count_err}",
                "speakers_indexed": 0,
            }

        if rebuild_count < len(docs_to_index):
            _drop_rebuild_index(rebuild_index)
            logger.error(
                f"Rebuild: {rebuild_index} holds {rebuild_count} of {len(docs_to_index)} "
                f"expected document(s); leaving '{speaker_index}' untouched."
            )
            return {
                "status": "error",
                "message": (
                    f"Rebuild incomplete: {rebuild_count} of {len(docs_to_index)} documents "
                    f"loaded. '{speaker_index}' was not deleted."
                ),
                "speakers_indexed": 0,
            }

        if rebuild_count == 0 and not allow_empty_rebuild:
            _drop_rebuild_index(rebuild_index)
            logger.error(
                f"Rebuild: nothing to restore for '{speaker_index}' — refusing to delete it. "
                "Recover from a snapshot, or pass allow_empty_rebuild=True to accept the "
                "loss of every voiceprint."
            )
            return {
                "status": "refused",
                "message": (
                    "No embeddings could be recovered, so rebuilding would erase every "
                    f"voiceprint. '{speaker_index}' was left in place."
                ),
                "speakers_indexed": 0,
            }

        # Step 6: Delete the corrupted speakers index
        if _client.opensearch_client.indices.exists(index=speaker_index):
            _client.opensearch_client.indices.delete(index=speaker_index)
            logger.info(f"Rebuild: deleted corrupted index {speaker_index}")

        # Step 7: Create new speakers index with correct mapping
        _client.opensearch_client.indices.create(index=speaker_index, body=speaker_index_config)
        logger.info(f"Rebuild: created fresh index {speaker_index}")

        # Step 8: Copy data from rebuild index to new speakers index
        copy_count = 0
        if rebuild_count > 0:
            # Read all docs from rebuild index and bulk-index into new speakers index
            search_after = None
            while True:
                query = {
                    "size": 500,
                    "query": {"match_all": {}},
                    "sort": [{"_id": "asc"}],
                }
                if search_after:
                    query["search_after"] = search_after

                response = _client.opensearch_client.search(index=rebuild_index, body=query)
                hits = response["hits"]["hits"]
                if not hits:
                    break

                copy_bulk: list[dict[str, Any]] = []
                for hit in hits:
                    copy_bulk.append(
                        {
                            "index": {
                                "_index": speaker_index,
                                "_id": hit["_id"],
                            }
                        }
                    )
                    copy_bulk.append(hit["_source"])

                resp = _client.opensearch_client.bulk(body=copy_bulk, refresh="wait_for")
                if not resp.get("errors"):
                    copy_count += len(hits)

                search_after = hits[-1]["sort"]

            logger.info(f"Rebuild: copied {copy_count} docs to new {speaker_index}")

        # Step 9: Clean up rebuild index — ONLY once the copy is known complete.
        # Deleting it after a short copy would discard the only full copy that
        # exists, since the corrupted original is already gone by now.
        if copy_count < rebuild_count:
            logger.error(
                f"Rebuild: copied only {copy_count} of {rebuild_count} docs into "
                f"'{speaker_index}'. KEEPING '{rebuild_index}' — it holds the only "
                "complete copy of these embeddings."
            )
            return {
                "status": "error",
                "message": (
                    f"Copy back incomplete: {copy_count} of {rebuild_count} documents. "
                    f"The complete set is preserved in '{rebuild_index}'."
                ),
                "speakers_indexed": copy_count,
                "recovery_index": rebuild_index,
            }

        _drop_rebuild_index(rebuild_index)
        logger.info(f"Speaker index rebuild complete: {copy_count} speakers re-indexed")
        return {
            "status": "rebuilt",
            "speakers_indexed": copy_count,
        }

    except Exception as e:
        # Clean up the rebuild index only while the original still exists. Once
        # the corrupted speakers index has been deleted, this temporary index is
        # the last copy of every recovered embedding and deleting it on the way
        # out of an error path is the data loss, not the cleanup.
        try:
            if _client.opensearch_client.indices.exists(index=speaker_index):
                _drop_rebuild_index(rebuild_index)
            else:
                logger.error(
                    f"Speaker index rebuild failed after '{speaker_index}' was deleted. "
                    f"KEEPING '{rebuild_index}': it holds the recovered embeddings."
                )
        except Exception as cleanup_err:
            logger.debug("Failed to clean up rebuild index: %s", cleanup_err)
        logger.error(f"Speaker index rebuild failed: {e}")
        return {
            "status": "error",
            "message": str(e),
            "speakers_indexed": 0,
        }


def check_and_repair_indices() -> list[str]:
    """Check OpenSearch indices health and auto-repair corrupted shards.

    Checks:
    1. Query health. For an index declaring a ``knn_vector`` dimension this is a
       real kNN probe, not ``match_all`` — Lucene's text segments are crash-safe
       while its HNSW vector files are not, so a BM25 probe reports a healthy
       index while every vector query answers 503 (issue #540).
    2. Mapping correctness: speaker index embedding field is knn_vector, not float
       (catches dynamic mapping auto-creation with wrong type)

    ⚠️ **``transcript_chunks`` is deliberately NOT in this list.** Repairing the
    chunk plane needs ``ensure_chunks_index_exists`` and the reindex coordinator,
    both of which live in ``services/search`` — the layer *above* this package —
    so importing them here forms a cycle that makes mypy resolve
    ``indexing_service``'s view of this module to ``Any`` and silently deletes
    type checking across 66 call sites. Its health check lives in
    ``services/search/index_health.check_and_repair_chunks_index``, which the
    same two callers invoke immediately after this function (issue #540).

    Returns:
        List of index names that were repaired (empty if all healthy).
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping health check")
        return []

    v4_index = get_speaker_index_v4()
    indices = [
        get_speaker_index(),
        settings.OPENSEARCH_TRANSCRIPT_INDEX,
        v4_index,
    ]
    repaired: list[str] = []

    for index_name in indices:
        if not _client.opensearch_client.indices.exists(index=index_name):
            continue

        # Check 1: Mapping correctness for speaker indices
        if index_name in (get_speaker_index(), v4_index):
            try:
                mapping = _client.opensearch_client.indices.get_mapping(index=index_name)
                properties = mapping.get(index_name, {}).get("mappings", {}).get("properties", {})
                emb_type = properties.get("embedding", {}).get("type", "")
                if emb_type and emb_type != "knn_vector":
                    logger.warning(
                        f"Index {index_name} has wrong embedding type '{emb_type}' "
                        f"(expected knn_vector), triggering repair..."
                    )
                    if _repair_index(index_name):
                        repaired.append(index_name)
                    else:
                        logger.error(f"Index {index_name} mapping repair failed")
                    continue  # Skip query check — already handled
            except Exception as e:
                logger.warning(f"Mapping check failed for {index_name}: {e}")

        # Check 2: Query health, in the plane the index actually serves.
        #
        # Gated on ANN capability, not merely on a declared dimension: the
        # legacy `transcripts` index declares knn_vector WITHOUT a method, so an
        # ANN query there is rejected 400 with a message containing
        # `search_phase_execution_exception` — which `_is_index_corruption_error`
        # matches. Probing it would report a healthy index as corrupt and
        # rebuild it on every tick.
        if _client._supports_ann_search(index_name):
            probe = _client.probe_knn_health(index_name)
            if probe.is_serviceable:
                logger.info(f"kNN health check passed: {index_name} ({probe.status})")
                continue
            if not probe.is_corrupt:
                logger.warning(
                    f"kNN health check inconclusive for {index_name}: "
                    f"{probe.status} ({probe.detail})"
                )
                continue

            logger.error(f"Index {index_name} has a corrupted vector plane: {probe.detail}")
            if _repair_index(index_name):
                repaired.append(index_name)
            else:
                logger.error(f"Index {index_name} could not be repaired automatically")
            continue

        try:
            _client.opensearch_client.search(
                index=index_name, body={"query": {"match_all": {}}, "size": 0}
            )
            logger.info(f"Index health check passed: {index_name}")
        except Exception as e:
            if _is_index_corruption_error(e):
                logger.warning(f"Index {index_name} unhealthy, attempting repair: {e}")
                if _repair_index(index_name):
                    repaired.append(index_name)
                else:
                    logger.error(f"Index {index_name} could not be repaired automatically")
            else:
                logger.error(f"Index health check failed for {index_name}: {e}")

    return repaired
