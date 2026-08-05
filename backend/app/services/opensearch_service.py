import contextlib
import datetime
import logging
import time
from collections.abc import Generator
from typing import Any

from opensearchpy import OpenSearch
from opensearchpy import RequestsHttpConnection

from app.core.config import settings
from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3
from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
from app.core.constants import SENTENCE_TRANSFORMER_DIMENSION
from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v3
from app.core.constants import get_speaker_index_v4
from app.core.opensearch_auth import opensearch_connection_kwargs

# Setup logging
logger = logging.getLogger(__name__)


def _speaker_org_filter_clauses(organization_id: int | None) -> list[dict[str, Any]]:
    """Tenant-scope ``bool.filter`` clauses for speaker/voiceprint kNN queries.

    Delegates to the shared search-plane helper so the SQL, transcript-search,
    and speaker-search planes all encode tenancy identically. Org context -> a
    ``term`` on ``organization_id``; personal scope (None) -> exclude any doc
    that carries ``organization_id`` (so org voiceprints never match a personal
    search). Community-edition: callers pass None and docs are org-less, so the
    personal gate is a behavior-preserving no-op.
    """
    from app.services.search.tenant_scope import org_filter_clauses

    return org_filter_clauses(organization_id)


# Cache for get_active_speaker_index() — avoids hundreds of OpenSearch
# round-trips during batch re-clustering (#17).
_active_index_cache: tuple[str, float] | None = None
_ACTIVE_INDEX_CACHE_TTL = 30.0  # seconds

# Flag to avoid redundant ensure_indices_exist() calls during batch operations.
_indices_verified = False

# Lazy singleton for SentenceTransformer model (~80MB) — loaded once.
_sentence_transformer_model = None


def _get_sentence_transformer():
    """Lazy singleton for SentenceTransformer model."""
    global _sentence_transformer_model
    if _sentence_transformer_model is None:
        from sentence_transformers import SentenceTransformer

        _sentence_transformer_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _sentence_transformer_model


# Initialize the OpenSearch client (skipped when OPENSEARCH_ENABLED=false)
opensearch_client: OpenSearch | None
if not settings.OPENSEARCH_ENABLED:
    logger.info("OpenSearch is disabled (OPENSEARCH_ENABLED=false), skipping client initialization")
    opensearch_client = None
else:
    try:
        opensearch_client = OpenSearch(
            **opensearch_connection_kwargs(connection_class=RequestsHttpConnection)
        )
        logger.info("OpenSearch client initialized successfully")
    except (ConnectionError, ValueError) as e:
        logger.error(f"Configuration error initializing OpenSearch client: {e}")
        opensearch_client = None
    except Exception as e:
        logger.error(f"Unexpected error initializing OpenSearch client: {e}")
        opensearch_client = None


def get_opensearch_client() -> "OpenSearch | None":
    """Get the OpenSearch client, attempting lazy initialization if None.

    If the client was not initialized at module load time (e.g., OpenSearch
    was not yet available), this function attempts to create a new client.

    Returns:
        OpenSearch client instance, or None if connection fails.
    """
    global opensearch_client
    if opensearch_client is not None:
        return opensearch_client

    try:
        opensearch_client = OpenSearch(
            **opensearch_connection_kwargs(connection_class=RequestsHttpConnection)
        )
        logger.info("OpenSearch client lazily initialized successfully")
        return opensearch_client
    except Exception as e:
        logger.warning(f"Lazy OpenSearch client initialization failed: {e}")
        return None


def _is_alias(name: str) -> bool:
    """Check if a name is an alias (not a concrete index)."""
    if not opensearch_client:
        return False
    try:
        return bool(opensearch_client.indices.exists_alias(name=name))
    except Exception:
        return False


def _get_alias_target(alias_name: str) -> str | None:
    """Get the concrete index an alias points to. Returns None if not an alias."""
    if not opensearch_client:
        return None
    try:
        result = opensearch_client.indices.get_alias(name=alias_name)
        # result is {concrete_index_name: {aliases: {alias_name: {}}}}
        indices = list(result.keys())
        return indices[0] if indices else None
    except Exception:
        return None


def get_active_versioned_index() -> str:
    """Get the concrete versioned index name that the 'speakers' alias points to.

    Returns the alias target (e.g. 'speakers_v3' or 'speakers_v4'), or
    falls back to get_speaker_index() if aliases haven't been set up yet.
    """
    alias_name = get_speaker_index()
    target = _get_alias_target(alias_name)
    if target:
        return target
    # Fallback: alias not set up yet (pre-migration state)
    return alias_name


def get_write_index() -> str:
    """Get the correct index to write speaker embeddings to based on current mode.

    Writes always target the concrete versioned index, never the alias.
    This ensures we write to the correct dimension index.
    """
    from app.services.embedding_mode_service import EmbeddingModeService

    mode = EmbeddingModeService.get_current_mode()
    if mode == "v3":
        return get_speaker_index_v3()
    return get_speaker_index_v4()


def migrate_to_alias_based_indices() -> dict[str, Any]:
    """One-time migration: convert concrete 'speakers' index to alias-based scheme.

    For 0.3.3 users who have 'speakers' as a concrete v3 index:
    1. Rename 'speakers' → 'speakers_v3' (via reindex + delete, since OS has no rename)
    2. Create alias 'speakers' → 'speakers_v3'

    For post-finalization users who have 'speakers' as a concrete v4 index:
    1. Rename 'speakers' → 'speakers_v4'
    2. Create alias 'speakers' → 'speakers_v4'

    For users who already have the alias: no-op.
    For fresh installs: create 'speakers_v4' + alias.

    Returns dict with migration status details.
    """
    from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3
    from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
    from app.core.constants import get_speaker_index_v3_backup

    if not opensearch_client:
        return {"status": "skipped", "reason": "no_client"}

    alias_name = get_speaker_index()  # "speakers"
    v3_index = get_speaker_index_v3()  # "speakers_v3"
    v4_index = get_speaker_index_v4()  # "speakers_v4"
    v3_backup = get_speaker_index_v3_backup()  # "speakers_v3_backup"

    # Already migrated: alias exists
    if _is_alias(alias_name):
        target = _get_alias_target(alias_name)
        logger.info(f"Speaker index alias already set up: {alias_name} → {target}")
        return {"status": "already_migrated", "alias_target": target}

    # Check if 'speakers' exists as a concrete index
    concrete_exists = False
    with contextlib.suppress(Exception):
        concrete_exists = opensearch_client.indices.exists(index=alias_name)

    if not concrete_exists:
        # Fresh install OR the index was deleted. Check for versioned indices.
        v3_exists = _safe_index_exists(v3_index)
        v4_exists = _safe_index_exists(v4_index)
        backup_exists = _safe_index_exists(v3_backup)

        if v3_exists or v4_exists:
            # Versioned indices exist but no alias — create alias to whichever exists
            target = v4_index if v4_exists else v3_index
            opensearch_client.indices.put_alias(index=target, name=alias_name)
            logger.info(f"Created alias {alias_name} → {target} (found existing versioned index)")
            return {"status": "alias_created", "alias_target": target}

        if backup_exists:
            # Only the v3 backup exists — rename it to speakers_v3 and alias
            _reindex_and_alias(v3_backup, v3_index, alias_name)
            logger.info(f"Restored from backup: {v3_backup} → {v3_index}, alias {alias_name}")
            return {"status": "restored_from_backup", "alias_target": v3_index}

        # Truly fresh install — will be handled by ensure_indices_exist()
        return {"status": "fresh_install"}

    # 'speakers' is a concrete index — need to detect its dimension and rename
    dimension = _get_index_embedding_dimension(alias_name)
    doc_count = 0
    with contextlib.suppress(Exception):
        doc_count = opensearch_client.count(index=alias_name)["count"]

    if dimension == PYANNOTE_EMBEDDING_DIMENSION_V3 or dimension == 512:
        target_index = v3_index
        mode_label = "v3"
    elif dimension == PYANNOTE_EMBEDDING_DIMENSION_V4 or dimension == 256:
        target_index = v4_index
        mode_label = "v4"
    elif doc_count == 0:
        # Empty index with unknown dimension — delete and let ensure_indices_exist handle it
        opensearch_client.indices.delete(index=alias_name)
        logger.info(f"Deleted empty concrete '{alias_name}' index (no dimension detected)")
        return {"status": "deleted_empty"}
    else:
        # Unknown dimension with data — default to v3 to be safe
        target_index = v3_index
        mode_label = "v3 (fallback)"

    # Check if the target versioned index already exists
    if _safe_index_exists(target_index):
        # Both concrete 'speakers' and versioned index exist — check which has more data
        target_count = 0
        with contextlib.suppress(Exception):
            target_count = opensearch_client.count(index=target_index)["count"]

        if target_count >= doc_count:
            # Versioned index has more/equal data — just delete concrete and alias
            opensearch_client.indices.delete(index=alias_name)
            opensearch_client.indices.put_alias(index=target_index, name=alias_name)
            logger.info(
                f"Alias migration: deleted concrete '{alias_name}' ({doc_count} docs), "
                f"aliased to existing '{target_index}' ({target_count} docs)"
            )
            return {"status": "migrated", "alias_target": target_index, "mode": mode_label}
        else:
            # Concrete has more data — reindex concrete into versioned, then alias
            logger.info(
                f"Concrete '{alias_name}' has more data ({doc_count}) than "
                f"'{target_index}' ({target_count}), merging"
            )
            opensearch_client.indices.delete(index=target_index)
            _reindex_and_alias(alias_name, target_index, alias_name)
            return {"status": "migrated_merged", "alias_target": target_index, "mode": mode_label}
    else:
        # Simple case: rename concrete → versioned, create alias
        _reindex_and_alias(alias_name, target_index, alias_name)
        logger.info(
            f"Alias migration: '{alias_name}' ({doc_count} docs, {mode_label}) "
            f"→ '{target_index}', alias created"
        )
        return {"status": "migrated", "alias_target": target_index, "mode": mode_label}


def _safe_index_exists(index_name: str) -> bool:
    """Check if an index exists, returning False on any error."""
    if not opensearch_client:
        return False
    try:
        return bool(opensearch_client.indices.exists(index=index_name))
    except Exception:
        return False


def _get_index_embedding_dimension(index_name: str) -> int | None:
    """Get the knn_vector dimension from an index's mapping."""
    if not opensearch_client:
        return None
    try:
        mapping = opensearch_client.indices.get_mapping(index=index_name)
        props = mapping.get(index_name, {}).get("mappings", {}).get("properties", {})
        emb = props.get("embedding", {})
        dim = emb.get("dimension")
        return int(dim) if dim is not None else None
    except Exception:
        return None


def _reindex_and_alias(source_index: str, target_index: str, alias_name: str) -> None:
    """Reindex source → target, delete source, create alias on target.

    This is used to "rename" a concrete index since OpenSearch has no
    native rename operation.
    """
    if not opensearch_client:
        return

    # Copy the mapping from source to create target with correct settings
    try:
        source_mapping = opensearch_client.indices.get(index=source_index)
        source_settings = source_mapping[source_index].get("settings", {}).get("index", {})
        source_mappings = source_mapping[source_index].get("mappings", {})

        # Build target index config from source (strip read-only settings)
        target_config: dict[str, Any] = {
            "settings": {
                "index": {
                    "number_of_shards": source_settings.get("number_of_shards", 1),
                    "number_of_replicas": source_settings.get("number_of_replicas", 0),
                }
            },
            "mappings": source_mappings,
        }
        # Preserve knn setting if present
        if source_settings.get("knn") == "true" or source_settings.get("knn") is True:
            target_config["settings"]["index"]["knn"] = True

        opensearch_client.indices.create(index=target_index, body=target_config)
    except Exception as e:
        logger.warning(f"Could not create target from source mapping: {e}. Trying plain reindex.")

    # Reindex data
    opensearch_client.reindex(
        body={"source": {"index": source_index}, "dest": {"index": target_index}},
        wait_for_completion=True,
    )

    # Verify
    source_count = opensearch_client.count(index=source_index)["count"]
    target_count = opensearch_client.count(index=target_index)["count"]
    if target_count < source_count * 0.95:
        raise RuntimeError(
            f"Reindex verification failed: {source_index} has {source_count} docs "
            f"but {target_index} only has {target_count}"
        )

    # Delete source and create alias
    opensearch_client.indices.delete(index=source_index)
    opensearch_client.indices.put_alias(index=target_index, name=alias_name)

    logger.info(
        f"Reindexed {source_index} → {target_index} ({target_count} docs), "
        f"alias '{alias_name}' created"
    )


def swap_speaker_alias(new_target: str) -> dict[str, Any]:
    """Atomically swap the 'speakers' alias to point to a new index.

    Uses the OpenSearch aliases API for an atomic swap — no downtime,
    no data copying. This is the correct way to "finalize" a migration.

    Args:
        new_target: The versioned index name to point the alias to.

    Returns:
        Dict with old_target and new_target.
    """
    if not opensearch_client:
        return {"status": "error", "reason": "no_client"}

    alias_name = get_speaker_index()
    old_target = _get_alias_target(alias_name)

    actions: list[dict] = []

    # Remove alias from old target (if any)
    if old_target:
        actions.append({"remove": {"index": old_target, "alias": alias_name}})

    # Add alias to new target
    actions.append({"add": {"index": new_target, "alias": alias_name}})

    opensearch_client.indices.update_aliases(body={"actions": actions})
    invalidate_active_speaker_index_cache()

    logger.info(f"Swapped speaker alias: {alias_name} → {new_target} (was: {old_target})")
    return {"status": "success", "old_target": old_target, "new_target": new_target}


def get_speaker_embedding_dimension() -> int:
    """
    Get the speaker embedding dimension from the existing index, or default to v4 for new installs.

    Returns:
        512 for v3 mode (existing pyannote/embedding data)
        256 for v4 mode (new WeSpeaker data or fresh install)
    """
    from app.services.embedding_mode_service import EmbeddingModeService

    return EmbeddingModeService.get_embedding_dimension()


def get_active_speaker_index() -> str:
    """Return the index to use for speaker embedding reads/searches.

    With the alias-based scheme, this always returns the 'speakers' alias
    which resolves to the correct versioned index. During v4 migration
    (before finalization), it checks if v4 has more data and returns that
    instead, since the alias still points to v3.

    Results are cached for 30s to avoid hundreds of round-trips during
    batch re-clustering.
    """
    global _active_index_cache

    now = time.monotonic()
    if _active_index_cache is not None:
        cached_index, cached_at = _active_index_cache
        if now - cached_at < _ACTIVE_INDEX_CACHE_TTL:
            return cached_index

    main_index = get_speaker_index()  # alias or concrete
    v4_index = get_speaker_index_v4()

    if not opensearch_client:
        return main_index

    result = main_index
    try:
        # If alias is set up, check if v4 staging has more data (pre-finalization)
        main_count = 0
        v4_count = 0

        # Count through alias (resolves to active versioned index)
        with contextlib.suppress(Exception):
            main_count = opensearch_client.count(index=main_index)["count"]

        if _safe_index_exists(v4_index):
            # Only check v4 if it's NOT the alias target (i.e., during pre-finalization)
            alias_target = _get_alias_target(main_index)
            if alias_target != v4_index:
                with contextlib.suppress(Exception):
                    v4_count = opensearch_client.count(index=v4_index)["count"]

                if v4_count > main_count:
                    logger.debug(
                        "Using v4 index '%s' (%d docs > %d in main)",
                        v4_index,
                        v4_count,
                        main_count,
                    )
                    result = v4_index

    except Exception as e:
        logger.debug("Error detecting active speaker index: %s", e)

    _active_index_cache = (result, now)
    return result


def invalidate_active_speaker_index_cache() -> None:
    """Force the next call to get_active_speaker_index() to re-query OpenSearch."""
    global _active_index_cache
    _active_index_cache = None


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
    if not opensearch_client:
        return False

    # Strategy 0: Detect wrong mapping type on speaker index embedding field.
    # If the index was auto-created by OpenSearch with dynamic mapping, the
    # embedding field will be "float" instead of "knn_vector". Close/reopen
    # and force-merge cannot fix this — the index must be deleted and recreated.
    if index_name == get_speaker_index():
        try:
            mapping = opensearch_client.indices.get_mapping(index=index_name)
            properties = mapping.get(index_name, {}).get("mappings", {}).get("properties", {})
            emb_type = properties.get("embedding", {}).get("type", "")
            if emb_type and emb_type != "knn_vector":
                logger.warning(
                    f"Speaker index '{index_name}' has embedding type '{emb_type}' "
                    f"instead of 'knn_vector'. Deleting and recreating with correct mapping."
                )
                doc_count = opensearch_client.count(index=index_name).get("count", 0)
                opensearch_client.indices.delete(index=index_name)
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
        opensearch_client.indices.close(index=index_name)
        opensearch_client.indices.open(index=index_name)
        opensearch_client.search(index=index_name, body={"query": {"match_all": {}}, "size": 0})
        logger.info(f"Index {index_name} repaired via close/reopen")
        return True
    except Exception as e:
        logger.warning(f"Close/reopen failed for {index_name}: {e}")

    # Strategy 2: Force merge to compact corrupted segments
    try:
        opensearch_client.indices.forcemerge(index=index_name, max_num_segments=1)
        opensearch_client.search(index=index_name, body={"query": {"match_all": {}}, "size": 0})
        logger.info(f"Index {index_name} repaired via force merge")
        return True
    except Exception as e:
        logger.warning(f"Force merge failed for {index_name}: {e}")

    # Strategy 3: For kNN indices, rebuild from PostgreSQL data (last resort)
    try:
        mapping = opensearch_client.indices.get_mapping(index=index_name)
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


def rebuild_speaker_index(db: "Any") -> dict[str, Any]:
    """Rebuild the speakers index from PostgreSQL + speakers_v4 data.

    Creates a temporary index, copies valid speaker data from the working
    speakers_v4 index, then swaps it in as the new speakers index. This is
    the nuclear option for when the speakers index has corrupted kNN segments
    that cannot be repaired via close/reopen or force-merge.

    Args:
        db: SQLAlchemy Session for querying Speaker rows.

    Returns:
        Dict with rebuild status and count of speakers indexed.
    """
    from sqlalchemy.orm import Session as SASession

    if not isinstance(db, SASession):
        return {"status": "error", "message": "Invalid database session", "speakers_indexed": 0}

    if not opensearch_client:
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
        if opensearch_client.indices.exists(index=v4_index):
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

                response = opensearch_client.search(index=v4_index, body=query)
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
        if opensearch_client.indices.exists(index=rebuild_index):
            opensearch_client.indices.delete(index=rebuild_index)

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

        opensearch_client.indices.create(index=rebuild_index, body=speaker_index_config)
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
                resp = opensearch_client.bulk(body=batch, refresh="wait_for")
                if not resp.get("errors"):
                    indexed_count += len(batch) // 2
                else:
                    # Count successes individually
                    for item in resp.get("items", []):
                        if item.get("index", {}).get("status") in (200, 201):
                            indexed_count += 1

        logger.info(f"Rebuild: indexed {indexed_count} speakers into {rebuild_index}")

        # Step 6: Delete the corrupted speakers index
        if opensearch_client.indices.exists(index=speaker_index):
            opensearch_client.indices.delete(index=speaker_index)
            logger.info(f"Rebuild: deleted corrupted index {speaker_index}")

        # Step 7: Create new speakers index with correct mapping
        opensearch_client.indices.create(index=speaker_index, body=speaker_index_config)
        logger.info(f"Rebuild: created fresh index {speaker_index}")

        # Step 8: Copy data from rebuild index to new speakers index
        if indexed_count > 0:
            # Read all docs from rebuild index and bulk-index into new speakers index
            search_after = None
            copy_count = 0
            while True:
                query = {
                    "size": 500,
                    "query": {"match_all": {}},
                    "sort": [{"_id": "asc"}],
                }
                if search_after:
                    query["search_after"] = search_after

                response = opensearch_client.search(index=rebuild_index, body=query)
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

                resp = opensearch_client.bulk(body=copy_bulk, refresh="wait_for")
                if not resp.get("errors"):
                    copy_count += len(hits)

                search_after = hits[-1]["sort"]

            logger.info(f"Rebuild: copied {copy_count} docs to new {speaker_index}")

        # Step 9: Clean up rebuild index
        if opensearch_client.indices.exists(index=rebuild_index):
            opensearch_client.indices.delete(index=rebuild_index)
            logger.info(f"Rebuild: deleted temporary index {rebuild_index}")

        logger.info(f"Speaker index rebuild complete: {indexed_count} speakers re-indexed")
        return {
            "status": "rebuilt",
            "speakers_indexed": indexed_count,
        }

    except Exception as e:
        # Clean up rebuild index on failure
        try:
            if opensearch_client.indices.exists(index=rebuild_index):
                opensearch_client.indices.delete(index=rebuild_index)
        except Exception as cleanup_err:
            logger.debug("Failed to clean up rebuild index: %s", cleanup_err)
        logger.error(f"Speaker index rebuild failed: {e}")
        return {
            "status": "error",
            "message": str(e),
            "speakers_indexed": 0,
        }


def _is_index_corruption_error(error: Exception) -> bool:
    """Check if an exception indicates OpenSearch index corruption or wrong mapping."""
    error_str = str(error).lower()
    return any(
        indicator in error_str
        for indicator in [
            "503",
            "search_phase_execution_exception",
            "already_closed",
            "no_shard_available",
            "not knn_vector type",
        ]
    )


def check_and_repair_indices() -> list[str]:
    """Check OpenSearch indices health and auto-repair corrupted shards.

    Checks:
    1. Query health: match_all query succeeds (catches 503, corrupted segments)
    2. Mapping correctness: speaker index embedding field is knn_vector, not float
       (catches dynamic mapping auto-creation with wrong type)

    Returns:
        List of index names that were repaired (empty if all healthy).
    """
    if not opensearch_client:
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
        if not opensearch_client.indices.exists(index=index_name):
            continue

        # Check 1: Mapping correctness for speaker indices
        if index_name in (get_speaker_index(), v4_index):
            try:
                mapping = opensearch_client.indices.get_mapping(index=index_name)
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

        # Check 2: Query health
        try:
            opensearch_client.search(index=index_name, body={"query": {"match_all": {}}, "size": 0})
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


def _ensure_versioned_speaker_index(index_name: str, dimension: int) -> None:
    """Create a versioned speaker index if it doesn't exist."""
    if not opensearch_client:
        return
    try:
        if opensearch_client.indices.exists(index=index_name):
            return
        config = {
            "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0, "knn": True}},
            "mappings": {
                "properties": {
                    "document_type": {"type": "keyword"},
                    "speaker_id": {"type": "integer"},
                    "speaker_uuid": {"type": "keyword"},
                    "profile_id": {"type": "integer"},
                    "profile_uuid": {"type": "keyword"},
                    "profile_name": {"type": "keyword"},
                    "user_id": {"type": "integer"},
                    # Tenant scope (cloud-edition seam; absent on personal docs)
                    "organization_id": {"type": "integer"},
                    "name": {"type": "keyword"},
                    "display_name": {"type": "keyword"},
                    "collection_ids": {"type": "integer"},
                    "media_file_id": {"type": "integer"},
                    "segment_count": {"type": "integer"},
                    "speaker_count": {"type": "integer"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": dimension,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "lucene",
                            "parameters": {"ef_construction": 128, "m": 24},
                        },
                    },
                }
            },
        }
        opensearch_client.indices.create(index=index_name, body=config)
        logger.info(f"Created versioned speaker index: {index_name} (dim={dimension})")
    except Exception as e:
        logger.error(f"Error creating speaker index {index_name}: {e}")


def ensure_indices_exist():
    """
    Ensure the transcript and speaker indices exist, creating them if necessary.

    For speaker indices, uses an alias-based scheme:
    - speakers_v3: concrete index with 512-dim embeddings
    - speakers_v4: concrete index with 256-dim embeddings
    - speakers: alias pointing to whichever is active
    """
    if not settings.OPENSEARCH_ENABLED:
        return
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping index creation")
        return

    try:
        # Create transcript index if it doesn't exist
        if not opensearch_client.indices.exists(index=settings.OPENSEARCH_TRANSCRIPT_INDEX):
            transcript_index_config = {
                "settings": {
                    "index": {"number_of_shards": 1, "number_of_replicas": 0},
                    "analysis": {"analyzer": {"default": {"type": "standard"}}},
                },
                "mappings": {
                    "properties": {
                        "file_id": {"type": "integer"},
                        "file_uuid": {"type": "keyword"},
                        "user_id": {"type": "integer"},
                        "content": {"type": "text"},
                        "speakers": {"type": "keyword"},
                        "tags": {"type": "keyword"},
                        "upload_time": {"type": "date"},
                        "title": {"type": "text"},
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": SENTENCE_TRANSFORMER_DIMENSION,
                        },
                    }
                },
            }

            opensearch_client.indices.create(
                index=settings.OPENSEARCH_TRANSCRIPT_INDEX, body=transcript_index_config
            )

            logger.info(f"Created transcript index: {settings.OPENSEARCH_TRANSCRIPT_INDEX}")

        # Migrate concrete 'speakers' index to alias-based scheme (0.3.3 upgrade)
        migration_result = migrate_to_alias_based_indices()
        if migration_result.get("status") not in ("already_migrated", "skipped"):
            logger.info(f"Speaker index alias migration: {migration_result}")

        # Ensure the versioned speaker indices exist
        from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3

        v3_index = get_speaker_index_v3()
        _ensure_versioned_speaker_index(v3_index, PYANNOTE_EMBEDDING_DIMENSION_V3)
        _ensure_versioned_speaker_index(get_speaker_index_v4(), PYANNOTE_EMBEDDING_DIMENSION_V4)

        # Restore v3 data from legacy backup if speakers_v3 is empty
        from app.core.constants import get_speaker_index_v3_backup

        v3_backup = get_speaker_index_v3_backup()
        if _safe_index_exists(v3_backup) and _safe_index_exists(v3_index):
            v3_count = 0
            with contextlib.suppress(Exception):
                v3_count = opensearch_client.count(index=v3_index)["count"]
            if v3_count == 0:
                backup_count = 0
                with contextlib.suppress(Exception):
                    backup_count = opensearch_client.count(index=v3_backup)["count"]
                if backup_count > 0:
                    try:
                        # Filter: only reindex docs with valid embeddings that
                        # match the v3 dimension (512). Docs with null embeddings
                        # or wrong dimensions would fail knn_vector parsing.
                        result = opensearch_client.reindex(
                            body={
                                "source": {
                                    "index": v3_backup,
                                    "query": {
                                        "bool": {
                                            "must": [{"exists": {"field": "embedding"}}],
                                            "must_not": [{"term": {"embedding": []}}],
                                        }
                                    },
                                },
                                "dest": {"index": v3_index},
                            },
                            wait_for_completion=True,
                        )
                        restored = result.get("created", 0) + result.get("updated", 0)
                        failures = len(result.get("failures", []))
                        logger.info(
                            f"Restored {restored} docs from '{v3_backup}' → '{v3_index}' "
                            f"({failures} failures, backup had {backup_count} docs)"
                        )
                        if failures > 0:
                            # Log first few failures for debugging
                            for f in result.get("failures", [])[:3]:
                                logger.warning(
                                    f"Reindex failure: {f.get('cause', {}).get('reason', f)}"
                                )
                    except Exception as e:
                        logger.warning(f"Failed to restore v3 backup: {e}")

        # Ensure the 'speakers' alias exists pointing to something
        alias_name = get_speaker_index()
        if not _is_alias(alias_name) and not _safe_index_exists(alias_name):
            # Default to v4 for fresh installs
            target = get_speaker_index_v4()
            if _safe_index_exists(target):
                opensearch_client.indices.put_alias(index=target, name=alias_name)
                logger.info(f"Created default alias: {alias_name} → {target}")

    except ConnectionError as e:
        logger.error(f"Connection error creating indices: {e}")
    except ValueError as e:
        logger.error(f"Configuration error creating indices: {e}")
    except Exception as e:
        logger.error(f"Unexpected error creating indices: {e}")


def create_speaker_index_v4(index_name: str | None = None) -> bool:
    """
    Create a new speaker index with v4 dimensions (256-dim).
    Used for migration from v3 to v4.

    Args:
        index_name: Name for the new index (defaults to speakers_v4)

    Returns:
        True if successful
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    index_name = index_name or get_speaker_index_v4()

    try:
        # Check if index already exists
        if opensearch_client.indices.exists(index=index_name):
            logger.info(f"V4 speaker index already exists: {index_name}")
            return True

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
                    "document_type": {"type": "keyword"},
                    "speaker_id": {"type": "integer"},
                    "speaker_uuid": {"type": "keyword"},
                    "profile_id": {"type": "integer"},
                    "profile_uuid": {"type": "keyword"},
                    "profile_name": {"type": "keyword"},
                    "user_id": {"type": "integer"},
                    # Tenant scope (cloud-edition seam; absent on personal docs)
                    "organization_id": {"type": "integer"},
                    "name": {"type": "keyword"},
                    "display_name": {"type": "keyword"},
                    "collection_ids": {"type": "integer"},
                    "media_file_id": {"type": "integer"},
                    "segment_count": {"type": "integer"},
                    "speaker_count": {"type": "integer"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": PYANNOTE_EMBEDDING_DIMENSION_V4,  # 256-dim for v4
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

        opensearch_client.indices.create(index=index_name, body=speaker_index_config)
        logger.info(f"Created v4 speaker index: {index_name}")
        return True

    except Exception as e:
        logger.error(f"Error creating v4 speaker index: {e}")
        return False


def ensure_v4_index_exists() -> bool:
    """Ensure speakers_v4 index exists, creating if needed. Idempotent."""
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    v4_index = get_speaker_index_v4()
    try:
        if opensearch_client.indices.exists(index=v4_index):
            return True
    except Exception as e:
        logger.warning(f"Error checking v4 index existence: {e}")

    return create_speaker_index_v4()


def add_speaker_embedding_v4(
    speaker_id: int,
    speaker_uuid: str,
    user_id: int,
    name: str,
    embedding: list[float],
    profile_id: int | None = None,
    profile_uuid: str | None = None,
    collection_ids: list[int] | None = None,
    media_file_id: int | None = None,
    segment_count: int = 1,
    display_name: str | None = None,
    organization_id: int | None = None,
):
    """
    Add a speaker embedding to the v4 staging index during migration.

    This function indexes to the _v4 staging index instead of the main speaker index,
    allowing migration to proceed without affecting the production index.

    Args:
        speaker_id: ID of the speaker in the database (for internal queries)
        speaker_uuid: UUID of the speaker (used as document ID)
        user_id: ID of the user who owns the speaker profile
        name: Name of the speaker
        embedding: Vector embedding of the speaker's voice (256-dim for v4)
        profile_id: Optional speaker profile ID (for internal queries)
        profile_uuid: Optional speaker profile UUID
        collection_ids: Optional list of collection IDs
        media_file_id: Optional source media file ID
        segment_count: Number of segments used to create embedding
        display_name: Optional display name for the speaker
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping speaker embedding")
        return

    v4_index = get_speaker_index_v4()

    try:
        # Validate embedding before indexing
        if embedding is None:
            logger.error(f"Cannot index speaker {speaker_uuid}: embedding is None")
            return

        if not isinstance(embedding, list) or len(embedding) == 0:
            logger.error(f"Cannot index speaker {speaker_uuid}: invalid embedding format")
            return

        # Dimension safety check: v4 index expects 256-dim embeddings
        emb_len = len(embedding)
        if emb_len != PYANNOTE_EMBEDDING_DIMENSION_V4:
            logger.error(
                f"Cannot index speaker {speaker_uuid} to v4: dimension mismatch "
                f"{emb_len} != {PYANNOTE_EMBEDDING_DIMENSION_V4}"
            )
            return

        logger.info(
            f"Indexing speaker {speaker_uuid} (ID: {speaker_id}) to v4 index with embedding length: {emb_len}"
        )

        # Prepare document (organization_id only written for org files — personal
        # docs stay org-less to match the personal-scope search gate)
        doc = {
            "speaker_id": speaker_id,
            "speaker_uuid": str(speaker_uuid),
            "profile_id": profile_id,
            "profile_uuid": str(profile_uuid) if profile_uuid else None,
            "user_id": user_id,
            "name": name,
            "display_name": display_name,
            "collection_ids": collection_ids or [],
            "media_file_id": media_file_id,
            "segment_count": segment_count,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "embedding": embedding,
        }
        if organization_id is not None:
            doc["organization_id"] = organization_id

        # Index the document to the v4 staging index using UUID as document ID
        response = opensearch_client.index(
            index=v4_index,
            body=doc,
            id=str(speaker_uuid),  # Use speaker_uuid as document ID
        )

        logger.info(
            f"Indexed speaker embedding for speaker {speaker_uuid} (ID: {speaker_id}) to v4 index: {response}"
        )
        return response

    except Exception as e:
        logger.error(
            f"Error indexing speaker embedding to v4 for speaker {speaker_uuid} (ID: {speaker_id}): {e}"
        )


def bulk_add_speaker_embeddings_v4(embeddings_data: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Bulk-index v4 (256-dim) speaker embeddings in one OpenSearch round-trip.

    Every post-transcription run stores one centroid per detected speaker.
    A 10-speaker meeting previously hit 10 sequential ``index()`` calls
    against OpenSearch (~200-500 ms of round-trip overhead); this helper
    batches them into one bulk request. Phase 2 PR #9, item D14.

    The per-speaker dimension guard mirrors ``add_speaker_embedding_v4`` —
    malformed entries are skipped rather than failing the whole batch, so
    a single bad embedding can't block the rest from landing.

    Args:
        embeddings_data: List of dicts shaped like the kwargs of
            ``add_speaker_embedding_v4``. ``speaker_uuid``, ``speaker_id``,
            ``user_id``, ``name``, and ``embedding`` are required. All
            other fields default the same way as the single-write helper.

    Returns:
        The OpenSearch bulk response dict, or None when the client is
        unavailable or no valid entries remain after validation.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping bulk v4 speaker embeddings")
        return None
    if not embeddings_data:
        return None

    v4_index = get_speaker_index_v4()
    bulk_body: list[dict[str, Any]] = []
    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
    accepted = 0

    for data in embeddings_data:
        speaker_uuid = data.get("speaker_uuid")
        speaker_id = data.get("speaker_id")
        embedding = data.get("embedding")

        if speaker_uuid is None or speaker_id is None:
            logger.error("Bulk v4 add skipping entry: missing speaker_uuid/speaker_id")
            continue
        if not isinstance(embedding, list) or len(embedding) != PYANNOTE_EMBEDDING_DIMENSION_V4:
            logger.error(
                f"Bulk v4 add skipping speaker {speaker_uuid}: embedding dim "
                f"{len(embedding) if isinstance(embedding, list) else 'n/a'} "
                f"!= {PYANNOTE_EMBEDDING_DIMENSION_V4}"
            )
            continue

        bulk_body.append(
            {
                "index": {
                    "_index": v4_index,
                    "_id": str(speaker_uuid),
                }
            }
        )
        profile_uuid = data.get("profile_uuid")
        v4_doc: dict[str, Any] = {
            "speaker_id": speaker_id,
            "speaker_uuid": str(speaker_uuid),
            "profile_id": data.get("profile_id"),
            "profile_uuid": str(profile_uuid) if profile_uuid else None,
            "user_id": data["user_id"],
            "name": data["name"],
            "display_name": data.get("display_name"),
            "collection_ids": data.get("collection_ids") or [],
            "media_file_id": data.get("media_file_id"),
            "segment_count": data.get("segment_count", 1),
            "created_at": now_iso,
            "updated_at": now_iso,
            "embedding": embedding,
        }
        if data.get("organization_id") is not None:
            v4_doc["organization_id"] = data["organization_id"]
        bulk_body.append(v4_doc)
        accepted += 1

    if accepted == 0:
        return None

    try:
        response = opensearch_client.bulk(body=bulk_body)
        if response.get("errors"):
            logger.error(f"Bulk v4 speaker embedding indexing had errors: {response}")
        else:
            logger.info(
                f"Bulk-indexed {accepted} v4 speaker embeddings into {v4_index} in one request"
            )
        return response  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(f"Error bulk-indexing v4 speaker embeddings: {e}")
        return None


def store_profile_embedding_v4(
    profile_id: int,
    profile_uuid: str,
    profile_name: str,
    embedding: list[float],
    speaker_count: int,
    user_id: int,
    organization_id: int | None = None,
) -> bool:
    """Store consolidated profile embedding in the v4 staging index.

    Same document structure as store_profile_embedding() but targets
    speakers_v4 index for migration pre-population.

    Args:
        profile_id: ID of the speaker profile.
        profile_uuid: UUID of the speaker profile (used as document ID).
        profile_name: Name of the speaker profile.
        embedding: Averaged 256-dim embedding vector.
        speaker_count: Number of speakers contributing to this embedding.
        user_id: ID of the user who owns the profile.

    Returns:
        True if successful, False otherwise.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    v4_index = get_speaker_index_v4()

    try:
        doc = {
            "document_type": "profile",
            "profile_id": profile_id,
            "profile_uuid": str(profile_uuid),
            "profile_name": profile_name,
            "user_id": user_id,
            "embedding": embedding,
            "speaker_count": speaker_count,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if organization_id is not None:
            doc["organization_id"] = organization_id

        opensearch_client.index(
            index=v4_index,
            body=doc,
            id=f"profile_{profile_uuid}",
            refresh="wait_for",
        )

        logger.info(
            f"Stored v4 profile {profile_uuid} ({profile_name}) embedding "
            f"with {speaker_count} speakers"
        )
        return True

    except Exception as e:
        logger.error(f"Error storing v4 profile embedding for {profile_uuid}: {e}")
        return False


def index_transcript(
    file_id: int,
    file_uuid: str,
    user_id: int,
    transcript_text: str,
    speakers: list[str],
    title: str,
    tags: list[str] | None = None,
    embedding: list[float] | None = None,
):
    """
    Index a transcript in OpenSearch

    Args:
        file_id: ID of the media file (for internal queries)
        file_uuid: UUID of the media file (used as document ID)
        user_id: ID of the user who owns the file
        transcript_text: Full transcript text
        speakers: List of speaker names/IDs in the transcript
        title: Title of the media file (filename)
        tags: Optional list of tags associated with the file
        embedding: Optional vector embedding of the transcript (if not provided, we'd compute it)
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping indexing")
        return

    try:
        ensure_indices_exist()

        # Skip embedding if not provided - let OpenSearch handle text search without vector similarity
        if embedding is None:
            logger.info(
                f"No embedding provided for transcript {file_uuid}, indexing with text search only"
            )
            # Don't include embedding field when none is provided

        # Prepare document
        doc = {
            "file_id": file_id,
            "file_uuid": str(file_uuid),
            "user_id": user_id,
            "content": transcript_text,
            "speakers": speakers,
            "title": title,
            "tags": tags or [],
            "upload_time": datetime.datetime.now(datetime.UTC).isoformat(),  # ISO-8601 format
        }

        # Only include embedding if provided
        if embedding is not None:
            doc["embedding"] = embedding

        # Index the document using UUID as document ID
        response = opensearch_client.index(
            index=settings.OPENSEARCH_TRANSCRIPT_INDEX,
            body=doc,
            id=str(file_uuid),  # Use file_uuid as document ID
        )

        logger.info(f"Indexed transcript for file {file_uuid} (ID: {file_id}): {response}")
        return response

    except Exception as e:
        logger.error(f"Error indexing transcript for file {file_uuid} (ID: {file_id}): {e}")


def update_transcript_title(file_uuid: str, new_title: str):
    """
    Update the title of an indexed transcript in OpenSearch

    Args:
        file_uuid: UUID of the media file
        new_title: New title to update
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping title update")
        return

    try:
        # Update the document with the new title
        update_body = {"doc": {"title": new_title}}

        response = opensearch_client.update(
            index=settings.OPENSEARCH_TRANSCRIPT_INDEX,
            id=str(file_uuid),
            body=update_body,
        )

        logger.info(f"Updated transcript title for file {file_uuid}: {response}")
        return response

    except Exception as e:
        # If the document doesn't exist yet, that's okay - it will be indexed later
        if "not_found" in str(e).lower():
            logger.info(
                f"Document not found for file {file_uuid}, will be indexed when transcription completes"
            )
        else:
            logger.error(f"Error updating transcript title for file {file_uuid}: {e}")


def add_speaker_embedding(
    speaker_id: int,
    speaker_uuid: str,
    user_id: int,
    name: str,
    embedding: list[float],
    profile_id: int | None = None,
    profile_uuid: str | None = None,
    collection_ids: list[int] | None = None,
    media_file_id: int | None = None,
    segment_count: int = 1,
    display_name: str | None = None,
    target_index: str | None = None,
    organization_id: int | None = None,
):
    """
    Add a speaker embedding to OpenSearch with collection support

    Args:
        speaker_id: ID of the speaker in the database (for internal queries)
        speaker_uuid: UUID of the speaker (used as document ID)
        user_id: ID of the user who owns the speaker profile
        name: Name of the speaker
        embedding: Vector embedding of the speaker's voice
        profile_id: Optional speaker profile ID (for internal queries)
        profile_uuid: Optional speaker profile UUID
        collection_ids: Optional list of collection IDs
        media_file_id: Optional source media file ID
        segment_count: Number of segments used to create embedding
        display_name: Optional display name for the speaker
        target_index: Optional override index name (e.g. 'speakers_v3').
            Defaults to the 'speakers' alias when None.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping speaker embedding")
        return

    index_name = target_index or get_speaker_index()

    try:
        ensure_indices_exist()

        # Validate embedding before indexing
        if embedding is None:
            logger.error(f"Cannot index speaker {speaker_uuid}: embedding is None")
            return

        if not isinstance(embedding, list) or len(embedding) == 0:
            logger.error(f"Cannot index speaker {speaker_uuid}: invalid embedding format")
            return

        # Dimension safety check: prevent writing wrong-dimension vectors
        emb_len = len(embedding)
        if emb_len not in (PYANNOTE_EMBEDDING_DIMENSION_V3, PYANNOTE_EMBEDDING_DIMENSION_V4):
            logger.error(
                f"Cannot index speaker {speaker_uuid}: unexpected embedding dimension "
                f"{emb_len} (expected {PYANNOTE_EMBEDDING_DIMENSION_V3} or "
                f"{PYANNOTE_EMBEDDING_DIMENSION_V4})"
            )
            return

        logger.info(
            f"Indexing speaker {speaker_uuid} (ID: {speaker_id}) with embedding length: {emb_len}"
        )

        # Prepare document (organization_id only written for org files)
        doc = {
            "speaker_id": speaker_id,
            "speaker_uuid": str(speaker_uuid),
            "profile_id": profile_id,
            "profile_uuid": str(profile_uuid) if profile_uuid else None,
            "user_id": user_id,
            "name": name,
            "display_name": display_name,
            "collection_ids": collection_ids or [],
            "media_file_id": media_file_id,
            "segment_count": segment_count,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "embedding": embedding,
        }
        if organization_id is not None:
            doc["organization_id"] = organization_id

        # Index the document using UUID as document ID
        response = opensearch_client.index(
            index=index_name,
            body=doc,
            id=str(speaker_uuid),  # Use speaker_uuid as document ID
        )

        logger.info(
            f"Indexed speaker embedding for speaker {speaker_uuid} (ID: {speaker_id}) "
            f"to {index_name}: {response}"
        )
        return response

    except Exception as e:
        # Retry once for transient connection errors before falling through
        # to the index corruption check below
        if isinstance(e, (ConnectionError, OSError)):
            logger.warning(f"Transient error indexing speaker {speaker_uuid}, retrying once: {e}")
            import time as _time

            _time.sleep(0.5)
            try:
                response = opensearch_client.index(
                    index=index_name,
                    body=doc,
                    id=str(speaker_uuid),
                )
                logger.info(
                    f"Retry succeeded: indexed speaker {speaker_uuid} after transient error"
                )
                return response
            except Exception as retry_err:
                logger.error(f"Retry failed for speaker {speaker_uuid}: {retry_err}")
                # Fall through to index corruption check with the retry error
                e = retry_err
        if _is_index_corruption_error(e):
            logger.warning(
                f"Index corruption detected indexing speaker {speaker_uuid}, attempting repair..."
            )
            if _repair_index(index_name):
                try:
                    doc = {
                        "speaker_id": speaker_id,
                        "speaker_uuid": str(speaker_uuid),
                        "profile_id": profile_id,
                        "profile_uuid": str(profile_uuid) if profile_uuid else None,
                        "user_id": user_id,
                        "name": name,
                        "display_name": display_name,
                        "collection_ids": collection_ids or [],
                        "media_file_id": media_file_id,
                        "segment_count": segment_count,
                        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
                        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                        "embedding": embedding,
                    }
                    if organization_id is not None:
                        doc["organization_id"] = organization_id
                    response = opensearch_client.index(
                        index=index_name,
                        body=doc,
                        id=str(speaker_uuid),
                    )
                    logger.info(f"Retry succeeded: indexed speaker {speaker_uuid} after repair")
                    return response
                except Exception as retry_err:
                    logger.error(
                        f"Retry after repair failed for speaker {speaker_uuid}: {retry_err}"
                    )
        else:
            logger.error(
                f"Error indexing speaker embedding for speaker {speaker_uuid} (ID: {speaker_id}): {e}"
            )


def bulk_add_speaker_embeddings(embeddings_data: list[dict[str, Any]]):
    """
    Bulk add multiple speaker embeddings for efficient indexing

    Args:
        embeddings_data: List of embedding data dictionaries with speaker_uuid
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        ensure_indices_exist()

        # Prepare bulk operations
        bulk_body = []
        for data in embeddings_data:
            # Index action using UUID as document ID
            bulk_body.append(
                {
                    "index": {
                        "_index": get_speaker_index(),
                        "_id": str(data["speaker_uuid"]),
                    }
                }
            )

            # Document (organization_id only written for org files)
            doc_data: dict[str, Any] = {
                "speaker_id": data["speaker_id"],
                "speaker_uuid": str(data["speaker_uuid"]),
                "profile_id": data.get("profile_id"),
                "profile_uuid": str(data.get("profile_uuid")) if data.get("profile_uuid") else None,
                "user_id": data["user_id"],
                "name": data["name"],
                "collection_ids": data.get("collection_ids", []),
                "media_file_id": data.get("media_file_id"),
                "segment_count": data.get("segment_count", 1),
                "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                "embedding": data["embedding"],
            }
            if data.get("organization_id") is not None:
                doc_data["organization_id"] = data["organization_id"]
            bulk_body.append(doc_data)

        # Execute bulk operation
        response = opensearch_client.bulk(body=bulk_body)

        if response["errors"]:
            logger.error(f"Bulk indexing had errors: {response}")
        else:
            logger.info(f"Successfully bulk indexed {len(embeddings_data)} speaker embeddings")

        return response

    except Exception as e:
        logger.error(f"Error bulk indexing speaker embeddings: {e}")


def search_transcripts(  # noqa: C901
    query: str,
    user_id: int,
    speaker: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    use_semantic: bool = True,
) -> list[dict[str, Any]]:
    """
    Search for transcripts matching the query

    Args:
        query: Search query text
        user_id: ID of the user performing the search
        speaker: Optional speaker name to filter by
        tags: Optional list of tags to filter by
        limit: Maximum number of results to return
        use_semantic: Whether to use semantic (vector) search in addition to text search

    Returns:
        List of matching documents
    """
    # Return empty results when OpenSearch is disabled or unavailable
    if not settings.OPENSEARCH_ENABLED:
        logger.debug("OpenSearch is disabled, returning empty search results")
        return []
    if not opensearch_client:
        logger.debug("OpenSearch client not initialized, returning empty search results")
        return []

    try:
        # Build the search query
        must_conditions: list[dict[str, Any]] = [
            {"term": {"user_id": user_id}}  # Restrict to user's files
        ]

        # Add full-text search
        if query:
            must_conditions.append({"match": {"content": {"query": query, "fuzziness": "AUTO"}}})

        # Add speaker filter if specified
        if speaker:
            must_conditions.append({"term": {"speakers": speaker}})

        # Add tags filter if specified
        if tags and len(tags) > 0:
            must_conditions.append({"terms": {"tags": tags}})

        # Construct basic search
        search_body = {
            "query": {"bool": {"must": must_conditions}},
            "size": limit,
            "_source": [
                "file_id",
                "file_uuid",
                "title",
                "content",
                "speakers",
                "tags",
                "upload_time",
            ],
            "highlight": {
                "fields": {
                    "content": {
                        "pre_tags": ["<em>"],
                        "post_tags": ["</em>"],
                        "fragment_size": 150,
                    }
                }
            },
        }

        # Add semantic search if requested
        if use_semantic and query:
            # Compute the query embedding using sentence-transformers
            try:
                embedding_model = _get_sentence_transformer()

                # Generate embedding for the query
                query_embedding = embedding_model.encode(query, normalize_embeddings=True).tolist()
                logger.info(f"Generated embedding for query: {query[:30]}...")
            except ImportError:
                logger.warning(
                    "sentence-transformers package not installed, using fallback embedding"
                )
                # Fallback to zero vector
                query_embedding = [0.0] * SENTENCE_TRANSFORMER_DIMENSION
            except Exception as e:
                logger.warning(f"Error generating query embedding: {e}")
                # Fallback to zero vector
                query_embedding = [0.0] * SENTENCE_TRANSFORMER_DIMENSION

            # Add kNN query
            knn_query: dict[str, Any] = {
                "knn": {"embedding": {"vector": query_embedding, "k": limit}}
            }

            # Combine text search with vector search
            search_body_query = search_body["query"]
            if isinstance(search_body_query, dict) and "bool" in search_body_query:
                search_body_query["bool"]["should"] = [knn_query]

        # Execute search
        response = opensearch_client.search(
            index=settings.OPENSEARCH_TRANSCRIPT_INDEX, body=search_body
        )

        # Process results
        results = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            result = {
                "file_id": source["file_id"],
                "file_uuid": source.get("file_uuid"),
                "title": source["title"],
                "speakers": source["speakers"],
                "upload_time": source["upload_time"],
            }

            # Add highlighted snippet if available
            if "highlight" in hit and "content" in hit["highlight"]:
                result["snippet"] = "...".join(hit["highlight"]["content"])
            else:
                # Fallback to first part of content
                content = source.get("content", "")
                result["snippet"] = content[:150] + "..." if len(content) > 150 else content

            results.append(result)

        return results

    except Exception as e:
        logger.error(f"Error searching transcripts: {e}")
        return []


def _extract_speaker_match(hit: dict, threshold: float) -> dict[str, Any] | None:
    """Convert a single OpenSearch kNN hit to a speaker match dict.

    Converts the OpenSearch cosinesimil score ``(1 + cosine) / 2`` back to
    raw cosine similarity so thresholds represent real cosine values.
    """
    cosine_sim = 2.0 * hit["_score"] - 1.0
    if cosine_sim < threshold:
        return None

    source = hit["_source"]
    if "speaker_id" not in source:
        logger.debug(f"Skipping profile document in speaker matching: {source.get('profile_id')}")
        return None

    return {
        "speaker_id": source["speaker_id"],
        "speaker_uuid": source.get("speaker_uuid"),
        "profile_id": source.get("profile_id"),
        "profile_uuid": source.get("profile_uuid"),
        "name": source["name"],
        "confidence": cosine_sim,
        "media_file_id": source.get("media_file_id"),
        "collection_ids": source.get("collection_ids", []),
    }


def find_matching_speaker(
    embedding: list[float],
    user_id: int,
    threshold: float = 0.5,
    collection_ids: list[int] | None = None,
    exclude_speaker_ids: list[int] | None = None,
    accessible_user_ids: list[int] | None = None,
    organization_id: int | None = None,
) -> dict[str, Any] | None:
    """
    Find a matching speaker for a given embedding with confidence score

    Args:
        embedding: Speaker embedding vector
        user_id: ID of the user
        threshold: Minimum similarity threshold (0-1) for matching
        collection_ids: Optional list of collection IDs to search within
        exclude_speaker_ids: Optional list of speaker IDs to exclude
        accessible_user_ids: Optional list of user IDs to search within
            (for shared profile scope). If None, filters by user_id.
        organization_id: Active org id (None = personal). Adds the default-deny
            tenant gate so cross-org voiceprints never match. Community-edition
            invariance: None + org-less docs => the personal gate is a no-op.

    Returns:
        Dictionary with speaker info and confidence if a match is found, None otherwise
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping speaker matching")
        return None

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # Build filter conditions - use accessible user IDs for shared profile scope
        filters: list[dict[str, Any]]
        if accessible_user_ids:
            filters = [{"terms": {"user_id": accessible_user_ids}}]
        else:
            filters = [{"term": {"user_id": user_id}}]

        # Tenant gate (org term when set, else exclude org-stamped voiceprints).
        filters.extend(_speaker_org_filter_clauses(organization_id))

        # Add collection filter if specified
        if collection_ids:
            filters.append({"terms": {"collection_ids": collection_ids}})

        # Add exclusion filter if specified
        if exclude_speaker_ids:
            filters.append({"bool": {"must_not": {"terms": {"speaker_id": exclude_speaker_ids}}}})

        # Build a kNN query to find similar speaker embeddings
        # Using the proper OpenSearch knn query syntax based on documentation
        query = {
            "size": 5,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding,
                        "k": 5,
                        "filter": {"bool": {"filter": filters}},
                    }
                }
            },
        }

        # Execute search
        response = opensearch_client.search(index=get_speaker_index(), body=query)

        # Check if we have a match
        if len(response["hits"]["hits"]) > 0:
            match = _extract_speaker_match(response["hits"]["hits"][0], threshold)
            if match:
                return match

        # No match found or score below threshold
        return None

    except Exception as e:
        if _is_index_corruption_error(e):
            logger.warning(
                "Index corruption detected during speaker matching, attempting repair..."
            )
            if _repair_index(get_speaker_index()):
                try:
                    response = opensearch_client.search(index=get_speaker_index(), body=query)
                    if len(response["hits"]["hits"]) > 0:
                        match = _extract_speaker_match(response["hits"]["hits"][0], threshold)
                        if match:
                            return match
                    return None
                except Exception as retry_err:
                    logger.error(f"Retry after repair failed for speaker matching: {retry_err}")
                    return None
        logger.error(f"Error finding matching speaker: {e}")
        return None


def batch_find_matching_speakers(
    embeddings: list[dict[str, Any]],
    user_id: int,
    threshold: float = 0.5,
    max_candidates: int = 5,
    organization_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Find matching speakers for multiple embeddings in a single query (efficient batch operation)

    Args:
        embeddings: List of dicts with 'id' and 'embedding' keys
        user_id: ID of the user
        threshold: Minimum similarity threshold
        max_candidates: Maximum candidates per embedding
        organization_id: Active org id (None = personal) — tenant gate.

    Returns:
        List of match results for each input embedding
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # Use multi-search for efficient batch processing
        msearch_body: list[dict[str, Any]] = []
        org_clauses = _speaker_org_filter_clauses(organization_id)

        for emb_data in embeddings:
            # Add search header
            msearch_body.append({"index": get_speaker_index()})

            # Add search query with self-exclusion
            query_body: dict[str, Any] = {
                "size": max_candidates,
                "query": {
                    "bool": {
                        "filter": [{"term": {"user_id": user_id}}, *org_clauses],
                        "must_not": [
                            {"term": {"speaker_id": emb_data["id"]}},  # Exclude self
                            {"exists": {"field": "document_type"}},  # Exclude profile documents
                        ],
                    }
                },
                "knn": {
                    "embedding": {
                        "vector": emb_data["embedding"],
                        "k": max_candidates,
                    }
                },
            }
            msearch_body.append(query_body)

        # Execute multi-search
        response = opensearch_client.msearch(body=msearch_body)

        # Process results
        results = []
        for i, emb_data in enumerate(embeddings):
            search_response = response["responses"][i]

            matches = []
            if "hits" in search_response and search_response["hits"]["hits"]:
                for hit in search_response["hits"]["hits"]:
                    score = 2.0 * hit["_score"] - 1.0  # raw cosine
                    if score >= threshold:
                        source = hit["_source"]
                        matches.append(
                            {
                                "speaker_id": source["speaker_id"],
                                "speaker_uuid": source.get("speaker_uuid"),
                                "profile_id": source.get("profile_id"),
                                "profile_uuid": source.get("profile_uuid"),
                                "name": source["name"],
                                "confidence": score,
                                "media_file_id": source.get("media_file_id"),
                            }
                        )

            results.append({"input_id": emb_data["id"], "matches": matches})

        return results

    except Exception as e:
        logger.error(f"Error in batch speaker matching: {e}")
        return []


def find_speaker_across_media(speaker_uuid: str, user_id: int) -> list[dict[str, Any]]:
    """
    Find all media files where a specific speaker appears

    Args:
        speaker_uuid: UUID of the speaker
        user_id: ID of the user

    Returns:
        List of media files where this speaker appears
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # First, get the speaker's name from the speaker index using UUID
        speaker_doc = opensearch_client.get(index=get_speaker_index(), id=str(speaker_uuid))

        if not speaker_doc or "_source" not in speaker_doc:
            return []

        speaker_name = speaker_doc["_source"]["name"]

        # Search for transcripts containing this speaker
        size_limit = 100
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"user_id": user_id}},
                        {"term": {"speakers": speaker_name}},
                    ]
                }
            },
            "size": size_limit,  # Max media files returned; increase if users have very large libraries
            "_source": ["file_id", "file_uuid", "title", "upload_time"],
        }

        response = opensearch_client.search(index=settings.OPENSEARCH_TRANSCRIPT_INDEX, body=query)

        total_hits = response["hits"]["total"]["value"]
        if total_hits > size_limit:
            logger.warning(
                "Results truncated: %d hits but size limit is %d",
                total_hits,
                size_limit,
            )

        # Process results
        results = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            results.append(
                {
                    "file_id": source["file_id"],
                    "file_uuid": source.get("file_uuid"),
                    "title": source["title"],
                    "upload_time": source["upload_time"],
                }
            )

        return results

    except Exception as e:
        logger.error(f"Error finding speaker across media: {e}")
        return []


def update_speaker_collections(
    speaker_uuid: str, profile_id: int, profile_uuid: str, collection_ids: list[int]
):
    """
    Update speaker embedding collections when a speaker is labeled/assigned to profile

    Args:
        speaker_uuid: Speaker UUID
        profile_id: Profile ID the speaker is assigned to (for internal queries)
        profile_uuid: Profile UUID the speaker is assigned to
        collection_ids: List of collection IDs to assign
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        # Update the speaker document in OpenSearch using UUID
        update_body = {
            "doc": {
                "profile_id": profile_id,
                "profile_uuid": str(profile_uuid) if profile_uuid else None,
                "collection_ids": collection_ids,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        response = opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
        )

        logger.info(f"Updated speaker {speaker_uuid} collections: {collection_ids}")
        return response

    except Exception as e:
        logger.error(f"Error updating speaker collections: {e}")


def move_speaker_to_profile_collection(
    unlabeled_speaker_uuid: str,
    target_profile_id: int,
    target_profile_uuid: str,
    target_collection_ids: list[int],
):
    """
    Move an unlabeled speaker embedding to a profile's collection

    Args:
        unlabeled_speaker_uuid: UUID of the unlabeled speaker
        target_profile_id: ID of the target profile (for internal queries)
        target_profile_uuid: UUID of the target profile
        target_collection_ids: Target collection IDs
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        # Update the speaker's profile and collection assignments using UUID
        update_body = {
            "doc": {
                "profile_id": target_profile_id,
                "profile_uuid": str(target_profile_uuid) if target_profile_uuid else None,
                "collection_ids": target_collection_ids,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        response = opensearch_client.update(
            index=get_speaker_index(),
            id=str(unlabeled_speaker_uuid),
            body=update_body,
        )

        logger.info(f"Moved speaker {unlabeled_speaker_uuid} to profile {target_profile_uuid}")
        return response

    except Exception as e:
        logger.error(f"Error moving speaker to profile collection: {e}")


def bulk_update_collection_assignments(updates: list[dict[str, Any]]):
    """
    Bulk update collection assignments for multiple speakers

    Args:
        updates: List of update dictionaries with speaker_uuid, profile_id, profile_uuid, collection_ids
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        # Prepare bulk update operations
        bulk_body: list[dict[str, Any]] = []
        for update in updates:
            # Update action using UUID as document ID
            bulk_body.append(
                {
                    "update": {
                        "_index": get_speaker_index(),
                        "_id": str(update["speaker_uuid"]),
                    }
                }
            )

            # Update document
            doc_update: dict[str, Any] = {
                "doc": {
                    "profile_id": update.get("profile_id"),
                    "profile_uuid": str(update.get("profile_uuid"))
                    if update.get("profile_uuid")
                    else None,
                    "collection_ids": update.get("collection_ids", []),
                    "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                }
            }
            bulk_body.append(doc_update)

        # Execute bulk operation
        response = opensearch_client.bulk(body=bulk_body)

        if response["errors"]:
            logger.error(f"Bulk collection update had errors: {response}")
        else:
            logger.info(f"Successfully updated collections for {len(updates)} speakers")

        return response

    except Exception as e:
        logger.error(f"Error bulk updating collection assignments: {e}")


def get_speakers_in_collection(collection_id: int, user_id: int) -> list[dict[str, Any]]:
    """
    Get all speakers in a specific collection

    Args:
        collection_id: Collection ID
        user_id: User ID

    Returns:
        List of speaker documents in the collection
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        size_limit = 1000
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"user_id": user_id}},
                        {"term": {"collection_ids": collection_id}},
                    ]
                }
            },
            "size": size_limit,  # Upper bound for speakers per collection; log warning if exceeded
            "_source": [
                "speaker_id",
                "speaker_uuid",
                "profile_id",
                "profile_uuid",
                "name",
                "media_file_id",
                "segment_count",
                "created_at",
            ],
        }

        response = opensearch_client.search(index=get_speaker_index(), body=query)

        total_hits = response["hits"]["total"]["value"]
        if total_hits > size_limit:
            logger.warning(
                "Results truncated: %d hits but size limit is %d",
                total_hits,
                size_limit,
            )

        speakers = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            speakers.append(
                {
                    "speaker_id": source["speaker_id"],
                    "speaker_uuid": source.get("speaker_uuid"),
                    "profile_id": source.get("profile_id"),
                    "profile_uuid": source.get("profile_uuid"),
                    "name": source["name"],
                    "media_file_id": source.get("media_file_id"),
                    "segment_count": source.get("segment_count", 1),
                    "created_at": source.get("created_at"),
                }
            )

        return speakers

    except Exception as e:
        logger.error(f"Error getting speakers in collection: {e}")
        return []


def merge_speaker_embeddings(
    source_speaker_uuid: str, target_speaker_uuid: str, new_collection_ids: list[int]
):
    """
    Merge two speaker embeddings (used when combining speakers)

    Args:
        source_speaker_uuid: UUID of speaker to merge from
        target_speaker_uuid: UUID of speaker to merge into
        new_collection_ids: Updated collection IDs for the target
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        # Delete the source speaker document from main index
        opensearch_client.delete(index=get_speaker_index(), id=str(source_speaker_uuid))

        # Also remove source from v4 staging index if it exists (mid-migration cleanup)
        import contextlib as _ctx

        v4_index = get_speaker_index_v4()
        with _ctx.suppress(Exception):
            if opensearch_client.indices.exists(index=v4_index):
                opensearch_client.delete(index=v4_index, id=str(source_speaker_uuid))

        # Update the target speaker's collections using UUID
        update_body = {
            "doc": {
                "collection_ids": new_collection_ids,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        response = opensearch_client.update(
            index=get_speaker_index(),
            id=str(target_speaker_uuid),
            body=update_body,
        )

        logger.info(f"Merged speaker {source_speaker_uuid} into {target_speaker_uuid}")
        return response

    except Exception as e:
        logger.error(f"Error merging speaker embeddings: {e}")


def cleanup_orphaned_embeddings(user_id: int) -> dict:
    """Count potentially orphaned speaker embeddings.

    NOTE: This is a diagnostic stub. Actual cleanup requires database
    validation and is not yet implemented. Use
    ``cleanup_orphaned_speaker_embeddings()`` for real orphan removal.

    Args:
        user_id: User ID to inspect.

    Returns:
        Dict with embedding_count and diagnostic status.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return {"embedding_count": 0, "status": "diagnostic_only"}

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # Find all embeddings for user
        query = {
            "query": {"term": {"user_id": user_id}},
            "size": 1000,
            "_source": ["speaker_id", "profile_id"],
        }

        response = opensearch_client.search(index=get_speaker_index(), body=query)

        count = len(response["hits"]["hits"])
        logger.info(
            f"Found {count} embeddings for user {user_id} (diagnostic only, no cleanup performed)"
        )

        return {"embedding_count": count, "status": "diagnostic_only"}

    except Exception as e:
        logger.error(f"Error counting orphaned embeddings: {e}")
        return {"embedding_count": 0, "status": "diagnostic_only"}


def get_speaker_document(speaker_uuid: str) -> dict[str, Any] | None:
    """
    Get full speaker document (embedding + segment_count) from OpenSearch.

    Args:
        speaker_uuid: UUID of the speaker

    Returns:
        Dict with 'embedding' and 'segment_count' keys, or None if not found
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None

    try:
        ensure_indices_exist()

        response = opensearch_client.get(index=get_speaker_index(), id=str(speaker_uuid))

        if response and "_source" in response:
            source = response["_source"]
            embedding = source.get("embedding")
            if embedding is not None:
                return {
                    "embedding": list(embedding),
                    "segment_count": int(source.get("segment_count", 1)),
                }
            return None

        return None

    except Exception as e:
        logger.error(f"Error getting speaker document for {speaker_uuid}: {e}")
        return None


def get_speaker_embedding(speaker_uuid: str) -> list[float] | None:
    """Get the embedding vector for a speaker from the active index.

    Queries only the active speaker index (v3 or v4, whichever holds the
    bulk of embeddings).  No cross-index fallback — this guarantees all
    returned embeddings have the same dimensionality.

    Args:
        speaker_uuid: UUID of the speaker

    Returns:
        Embedding vector or None if not found
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None

    try:
        ensure_indices_exist()

        active_index = get_active_speaker_index()
        response = opensearch_client.get(index=active_index, id=str(speaker_uuid))

        if response and "_source" in response:
            embedding = response["_source"].get("embedding")
            if embedding is not None:
                return list(embedding)

        return None

    except Exception as e:
        if _is_index_corruption_error(e):
            logger.warning(
                f"Index corruption detected getting speaker {speaker_uuid}, attempting repair..."
            )
            active_index = get_active_speaker_index()
            if _repair_index(active_index):
                try:
                    response = opensearch_client.get(index=active_index, id=str(speaker_uuid))
                    if response and "_source" in response:
                        embedding = response["_source"].get("embedding")
                        if embedding is not None:
                            return list(embedding)
                except Exception as retry_err:
                    logger.error(
                        f"Retry after repair failed for speaker {speaker_uuid}: {retry_err}"
                    )
            return None
        # NotFoundError is expected for speakers not in active index
        if "NotFoundError" not in type(e).__name__:
            logger.error(f"Error getting speaker embedding: {e}")
        return None


def get_speaker_embeddings_batch(speaker_uuids: list[str]) -> dict[str, list[float]]:
    """Get embeddings for multiple speakers in a single mget request.

    Args:
        speaker_uuids: List of speaker UUIDs

    Returns:
        Dict mapping speaker_uuid -> embedding vector (only for found speakers)
    """
    if not opensearch_client or not speaker_uuids:
        return {}

    try:
        ensure_indices_exist()
        active_index = get_active_speaker_index()

        body = {"docs": [{"_index": active_index, "_id": str(uid)} for uid in speaker_uuids]}
        response = opensearch_client.mget(body=body)

        results: dict[str, list[float]] = {}
        for doc in response.get("docs", []):
            if doc.get("found") and "_source" in doc:
                embedding = doc["_source"].get("embedding")
                if embedding is not None:
                    results[doc["_id"]] = list(embedding)
        return results

    except Exception as e:
        logger.error(f"Error batch-fetching speaker embeddings: {e}")
        return {}


def msearch_profile_knn_batch(
    speaker_embeddings: dict[str, list[float]],
    user_id: int,
    threshold: float = 0.5,
    k: int = 10,
    accessible_profile_ids: set[int] | None = None,
    organization_id: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Batch kNN search for profile matches across multiple speakers.

    Executes a single msearch request containing one kNN query per speaker,
    each searching for matching profile documents.

    Args:
        speaker_embeddings: Dict mapping speaker_uuid -> embedding vector.
        user_id: Owner user ID for filtering.
        threshold: Minimum raw cosine similarity to include.
        k: Number of nearest neighbors per query.
        accessible_profile_ids: Optional set of profile IDs to restrict search.
        organization_id: Active org id (None = personal) — tenant gate.

    Returns:
        Dict mapping speaker_uuid -> list of profile match dicts.
    """
    if not opensearch_client or not speaker_embeddings:
        return {uid: [] for uid in speaker_embeddings}

    try:
        ensure_indices_exist()
        speaker_index = get_speaker_index()

        # Build filter (same for all queries)
        if accessible_profile_ids is not None:
            must_filters: list[dict[str, Any]] = [
                {"term": {"document_type": "profile"}},
                {"terms": {"profile_id": list(accessible_profile_ids)}},
            ]
        else:
            must_filters = [
                {"term": {"document_type": "profile"}},
                {"term": {"user_id": user_id}},
            ]
        # Tenant gate on profile documents (org term, else exclude org-stamped).
        must_filters.extend(_speaker_org_filter_clauses(organization_id))

        # Build msearch body
        msearch_body: list[dict[str, Any]] = []
        uuid_order: list[str] = []

        for speaker_uuid, embedding in speaker_embeddings.items():
            uuid_order.append(speaker_uuid)
            msearch_body.append({"index": speaker_index})
            msearch_body.append(
                {
                    "size": k,
                    "query": {
                        "knn": {
                            "embedding": {
                                "vector": embedding,
                                "k": k,
                                "filter": {"bool": {"must": must_filters}},
                            }
                        }
                    },
                }
            )

        response = opensearch_client.msearch(body=msearch_body)

        results: dict[str, list[dict[str, Any]]] = {}
        for i, speaker_uuid in enumerate(uuid_order):
            matches: list[dict[str, Any]] = []
            resp = response["responses"][i]
            for hit in resp.get("hits", {}).get("hits", []):
                score = 2.0 * hit["_score"] - 1.0  # Convert OS cosinesimil to raw cosine
                if score >= threshold:
                    source = hit["_source"]
                    matches.append(
                        {
                            "profile_id": source.get("profile_id"),
                            "profile_name": source.get("profile_name"),
                            "speaker_count": source.get("speaker_count"),
                            "similarity": score,
                        }
                    )
            results[speaker_uuid] = matches

        return results

    except Exception as e:
        logger.error(f"Error in batch profile kNN search: {e}")
        return {uid: [] for uid in speaker_embeddings}


def get_profile_embedding(profile_uuid: str) -> list[float] | None:
    """
    Get the embedding vector for a speaker profile from OpenSearch

    Args:
        profile_uuid: UUID of the speaker profile

    Returns:
        Embedding vector or None if not found
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # Use UUID-based document ID for profiles
        response = opensearch_client.get(index=get_speaker_index(), id=f"profile_{profile_uuid}")

        if response and "_source" in response:
            embedding = response["_source"].get("embedding")
            if embedding is not None:
                return list(embedding)  # Explicit conversion to list[float]
            return None

        return None

    except Exception as e:
        logger.error(f"Error getting profile embedding: {e}")
        return None


def store_profile_embedding(
    profile_id: int,
    profile_uuid: str,
    profile_name: str,
    embedding: list[float],
    speaker_count: int,
    user_id: int,
    organization_id: int | None = None,
) -> bool:
    """
    Store profile embedding with distinct document type for proper filtering.

    Args:
        profile_id: ID of the speaker profile (for internal queries)
        profile_uuid: UUID of the speaker profile (used as document ID)
        profile_name: Name of the speaker profile
        embedding: Embedding vector
        speaker_count: Number of speakers contributing to this embedding
        user_id: ID of the user who owns the profile
        organization_id: Active org id (None = personal). Only written for org
            profiles so personal docs stay org-less (matches the search gate).

    Returns:
        True if successful, False otherwise
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    try:
        ensure_indices_exist()

        doc = {
            "document_type": "profile",  # CRITICAL: Distinguish from speakers
            "profile_id": profile_id,
            "profile_uuid": str(profile_uuid),
            "profile_name": profile_name,
            "user_id": user_id,
            "embedding": embedding,
            "speaker_count": speaker_count,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if organization_id is not None:
            doc["organization_id"] = organization_id

        # Use UUID-based prefixed ID to avoid conflicts with speaker documents
        # Use refresh='wait_for' to ensure the update is immediately searchable
        # This prevents race conditions where voice_suggestions show stale profile names
        opensearch_client.index(
            index=get_speaker_index(),
            body=doc,
            id=f"profile_{profile_uuid}",
            refresh="wait_for",
        )

        logger.info(
            f"Stored profile {profile_uuid} ({profile_name}) embedding in OpenSearch with {speaker_count} speakers"
        )
        return True

    except Exception as e:
        logger.error(f"Error storing profile embedding: {e}")
        return False


def remove_profile_embedding(profile_uuid: str) -> bool:
    """Remove a profile embedding from all speaker indices (main + v4 staging).

    Profiles are stored with doc ID ``profile_{uuid}`` in both the main index
    and the v4 staging index. Both are cleaned here.

    Args:
        profile_uuid: UUID of the speaker profile

    Returns:
        True if the main index deletion succeeded, False otherwise
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    doc_id = f"profile_{profile_uuid}"
    success = False
    try:
        opensearch_client.delete(index=get_speaker_index(), id=doc_id)
        logger.info(f"Removed profile {profile_uuid} embedding from main index")
        success = True
    except Exception as e:
        logger.warning(f"Error removing profile embedding from main index (may not exist): {e}")

    # Also clean v4 staging index if it exists
    try:
        v4_index = get_speaker_index_v4()
        if opensearch_client.indices.exists(index=v4_index):
            opensearch_client.delete(index=v4_index, id=doc_id)
            logger.debug(f"Removed profile {profile_uuid} from v4 staging index")
    except Exception:  # nosec B110
        pass  # Non-fatal

    return success


def store_cluster_embedding(
    cluster_uuid: str,
    user_id: int,
    embedding: list[float],
    label: str | None = None,
    refresh: str | bool = "wait_for",
    organization_id: int | None = None,
) -> bool:
    """Store a cluster centroid embedding in OpenSearch.

    Args:
        cluster_uuid: UUID of the speaker cluster.
        user_id: Owner user ID.
        embedding: L2-normalized centroid embedding vector.
        label: Optional cluster label.
        refresh: Index refresh policy. Use ``False`` during batch operations
            and issue a single ``indices.refresh()`` at the end.
        organization_id: Tenant scope of the cluster (None = personal). Only
            written for org clusters — personal/community docs carry no
            ``organization_id`` field, matching the per-file speaker docs.

    Returns:
        True if successful, False otherwise.
    """
    global _indices_verified

    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    try:
        if not _indices_verified:
            ensure_indices_exist()
            _indices_verified = True
        active_index = get_active_speaker_index()

        doc = {
            "document_type": "cluster",
            "cluster_uuid": str(cluster_uuid),
            "user_id": user_id,
            "embedding": embedding,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if label:
            doc["label"] = label
        if organization_id is not None:
            doc["organization_id"] = organization_id

        opensearch_client.index(
            index=active_index,
            body=doc,
            id=f"cluster_{cluster_uuid}",
            refresh=refresh,
        )

        logger.info(f"Stored cluster {cluster_uuid} centroid in OpenSearch")
        return True

    except Exception as e:
        logger.error(f"Error storing cluster embedding: {e}")
        return False


def delete_cluster_embedding(cluster_uuid: str) -> bool:
    """Delete a cluster centroid from OpenSearch.

    Args:
        cluster_uuid: UUID of the speaker cluster.

    Returns:
        True if successful, False otherwise.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    try:
        active_index = get_active_speaker_index()
        opensearch_client.delete(
            index=active_index,
            id=f"cluster_{cluster_uuid}",
        )
        logger.info(f"Removed cluster {cluster_uuid} embedding from OpenSearch")
        return True

    except Exception as e:
        logger.warning(f"Error removing cluster embedding (may not exist): {e}")
        return False


def find_matching_clusters(
    embedding: list[float],
    user_id: int,
    k: int = 5,
    threshold: float = 0.75,
    organization_id: int | None = None,
) -> list[dict]:
    """Find matching cluster centroids for a speaker embedding using kNN.

    Args:
        embedding: L2-normalized speaker embedding vector.
        user_id: Owner user ID.
        k: Number of nearest neighbors.
        threshold: Minimum cosine similarity.
        organization_id: Active org id (None = personal) — tenant gate.

    Returns:
        List of dicts with cluster_uuid, similarity, label.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        active_index = get_active_speaker_index()
        query = {
            "size": k,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding,
                        "k": k,
                        "filter": {
                            "bool": {
                                "filter": [
                                    {"term": {"document_type": "cluster"}},
                                    {"term": {"user_id": user_id}},
                                    *_speaker_org_filter_clauses(organization_id),
                                ]
                            }
                        },
                    }
                }
            },
        }

        response = opensearch_client.search(index=active_index, body=query)

        matches = []
        for hit in response["hits"]["hits"]:
            score = hit["_score"]
            # OpenSearch Lucene engine with cosinesimil space returns:
            #   score = (1 + cosine_similarity) / 2
            # Convert back to raw cosine similarity for threshold comparison.
            cosine_sim = 2.0 * score - 1.0
            if cosine_sim < threshold:
                continue
            source = hit["_source"]
            matches.append(
                {
                    "cluster_uuid": source.get("cluster_uuid"),
                    "similarity": float(cosine_sim),
                    "label": source.get("label"),
                }
            )

        return matches

    except Exception as e:
        logger.error(f"Error finding matching clusters: {e}")
        return []


def msearch_speaker_similarities(
    speaker_data: list[dict],
    user_id: int,
    k: int = 10,
    batch_size: int = 50,
    organization_id: int | None = None,
) -> list[list[dict]]:
    """Batch kNN search for building a similarity graph.

    Args:
        speaker_data: List of dicts with speaker_uuid and embedding.
        user_id: Owner user ID.
        k: Number of nearest neighbors per query.
        batch_size: Number of speakers per msearch request.
        organization_id: Active org id (None = personal) — tenant gate.

    Returns:
        List of result lists, one per input embedding.
    """
    if not opensearch_client or not speaker_data:
        return [[] for _ in speaker_data]

    try:
        import json

        active_index = get_active_speaker_index()
        all_results: list[list[dict]] = []
        org_clauses = _speaker_org_filter_clauses(organization_id)

        # Process in batches to avoid memory issues
        for batch_start in range(0, len(speaker_data), batch_size):
            batch = speaker_data[batch_start : batch_start + batch_size]

            # Build msearch body for this batch
            body_parts: list[str] = []
            for sd in batch:
                header = {"index": active_index}
                query = {
                    "size": k,
                    "query": {
                        "knn": {
                            "embedding": {
                                "vector": sd["embedding"],
                                "k": k,
                                "filter": {
                                    "bool": {
                                        "filter": [
                                            {"term": {"user_id": user_id}},
                                            {
                                                "bool": {
                                                    "must_not": [
                                                        {"exists": {"field": "document_type"}},
                                                    ]
                                                }
                                            },
                                            *org_clauses,
                                        ]
                                    }
                                },
                            }
                        }
                    },
                }
                body_parts.append(json.dumps(header))
                body_parts.append(json.dumps(query))

            msearch_body = "\n".join(body_parts) + "\n"
            response = opensearch_client.msearch(body=msearch_body)

            for resp in response.get("responses", []):
                hits: list[dict] = []
                for hit in resp.get("hits", {}).get("hits", []):
                    source = hit.get("_source", {})
                    hits.append(
                        {
                            "speaker_uuid": source.get("speaker_uuid"),
                            "similarity": 2.0 * float(hit["_score"]) - 1.0,  # raw cosine
                            "speaker_id": source.get("speaker_id"),
                        }
                    )
                all_results.append(hits)

        return all_results

    except Exception as e:
        logger.error(f"Error in msearch speaker similarities: {e}")
        return [[] for _ in speaker_data]


def iter_speaker_embeddings(
    user_id: int,
    speaker_uuids: list[str] | None = None,
    batch_size: int = 200,
) -> Generator[list[dict[str, Any]], None, None]:
    """Yield batches of speaker embeddings from the active index.

    If *speaker_uuids* is provided, only those speakers are fetched (via
    mget).  Otherwise all non-cluster speaker docs for *user_id* are
    scrolled.  Embeddings are never accumulated — each batch is yielded
    and can be discarded by the caller.

    Yields:
        Lists of dicts with keys: speaker_uuid, embedding, speaker_id,
        profile_id, display_name.
    """
    if not opensearch_client:
        return

    active_index = get_active_speaker_index()

    if speaker_uuids is not None:
        # Fetch specific speakers via mget in batches
        for i in range(0, len(speaker_uuids), batch_size):
            chunk = speaker_uuids[i : i + batch_size]
            try:
                response = opensearch_client.mget(
                    index=active_index,
                    body={"ids": chunk},
                )
                batch: list[dict[str, Any]] = []
                for doc in response.get("docs", []):
                    if doc.get("found") and "_source" in doc:
                        source = doc["_source"]
                        if "embedding" in source and "speaker_uuid" in source:
                            batch.append(
                                {
                                    "speaker_uuid": source["speaker_uuid"],
                                    "embedding": source["embedding"],
                                    "speaker_id": source.get("speaker_id"),
                                    "profile_id": source.get("profile_id"),
                                    "display_name": source.get("display_name"),
                                }
                            )
                if batch:
                    yield batch
            except Exception as e:
                logger.warning("mget batch failed: %s", e)
                continue
    else:
        # Scroll all speakers for user
        search_after = None
        while True:
            query: dict = {
                "size": batch_size,
                "query": {
                    "bool": {
                        "filter": [{"term": {"user_id": user_id}}],
                        "must_not": [{"exists": {"field": "document_type"}}],
                    }
                },
                "sort": [{"_id": "asc"}],
                "_source": [
                    "speaker_uuid",
                    "embedding",
                    "speaker_id",
                    "profile_id",
                    "display_name",
                ],
            }
            if search_after is not None:
                query["search_after"] = search_after

            try:
                response = opensearch_client.search(index=active_index, body=query)
            except Exception as e:
                logger.error("Scroll failed: %s", e)
                break

            hits = response["hits"]["hits"]
            if not hits:
                break

            batch = []
            for hit in hits:
                source = hit["_source"]
                if "embedding" in source and "speaker_uuid" in source:
                    batch.append(
                        {
                            "speaker_uuid": source["speaker_uuid"],
                            "embedding": source["embedding"],
                            "speaker_id": source.get("speaker_id"),
                            "profile_id": source.get("profile_id"),
                            "display_name": source.get("display_name"),
                        }
                    )
            if batch:
                yield batch

            search_after = hits[-1]["sort"]
            if len(hits) < batch_size:
                break


def get_all_speaker_embeddings(
    user_id: int,
    page_size: int = 500,
) -> list[dict]:
    """Fetch all speaker embeddings for a user from OpenSearch.

    Uses search_after pagination to handle arbitrarily large result sets
    instead of a hard limit.

    Args:
        user_id: Owner user ID.
        page_size: Number of documents per page (scroll batch size).

    Returns:
        List of dicts with speaker_uuid and embedding.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        active_index = get_active_speaker_index()
        results: list[dict] = []
        search_after = None

        while True:
            query: dict = {
                "size": page_size,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"user_id": user_id}},
                        ],
                        "must_not": [
                            {"exists": {"field": "document_type"}},
                        ],
                    }
                },
                "sort": [{"_id": "asc"}],
                "_source": [
                    "speaker_uuid",
                    "embedding",
                    "speaker_id",
                    "profile_id",
                    "display_name",
                ],
            }

            if search_after is not None:
                query["search_after"] = search_after

            response = opensearch_client.search(index=active_index, body=query)

            hits = response["hits"]["hits"]
            if not hits:
                break

            for hit in hits:
                source = hit["_source"]
                if "embedding" in source and "speaker_uuid" in source:
                    results.append(
                        {
                            "speaker_uuid": source["speaker_uuid"],
                            "embedding": source["embedding"],
                            "speaker_id": source.get("speaker_id"),
                            "profile_id": source.get("profile_id"),
                            "display_name": source.get("display_name"),
                        }
                    )

            # Use the sort value of the last hit for search_after
            search_after = hits[-1]["sort"]

            # If we got fewer results than page_size, we're done
            if len(hits) < page_size:
                break

        logger.info(
            f"Fetched {len(results)} speaker embeddings for user {user_id} "
            f"from index '{active_index}'"
        )
        return results

    except Exception as e:
        logger.error(f"Error fetching speaker embeddings: {e}")
        return []


def remove_speaker_embedding(speaker_uuid: str) -> bool:
    """Remove a speaker embedding from all speaker indices (main + v4 staging).

    Cleans both the main speaker index (V3 or post-finalization V4) and the
    v4 staging index if it exists. This prevents orphaned entries when speakers
    are deleted or merged.

    Args:
        speaker_uuid: UUID of the speaker

    Returns:
        True if the main index deletion succeeded, False otherwise
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    success = False

    # Delete from all speaker indices (v3, v4, and alias target)
    indices_to_clean = {get_speaker_index(), get_speaker_index_v3(), get_speaker_index_v4()}
    for idx in indices_to_clean:
        try:
            if _safe_index_exists(idx) or _is_alias(idx):
                opensearch_client.delete(index=idx, id=str(speaker_uuid))
                logger.debug(f"Removed speaker {speaker_uuid} from {idx}")
                success = True
        except Exception:  # nosec B110
            pass  # Non-fatal: speaker may not exist in this index

    if success:
        logger.info(f"Removed speaker {speaker_uuid} from speaker indices")

    return success


def update_speaker_segment_count(speaker_uuid: str, segment_count: int) -> bool:
    """
    Update only the segment_count of a speaker in OpenSearch.

    Args:
        speaker_uuid: UUID of the speaker
        segment_count: New segment count value

    Returns:
        True if successful, False otherwise
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    try:
        update_body = {
            "doc": {
                "segment_count": segment_count,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
        )

        logger.info(f"Updated segment_count for speaker {speaker_uuid} to {segment_count}")
        return True

    except Exception as e:
        logger.warning(f"Error updating speaker segment count: {e}")
        return False


def update_speaker_display_name(speaker_uuid: str, display_name: str | None):
    """
    Update the display name of a speaker in OpenSearch

    Args:
        speaker_uuid: UUID of the speaker
        display_name: New display name (or None to clear)
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        # Update the speaker document with new display name using UUID
        update_body = {
            "doc": {
                "display_name": display_name,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        }

        response = opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
            refresh="wait_for",
        )

        logger.info(f"Updated display name for speaker {speaker_uuid} to '{display_name}'")
        return response

    except Exception as e:
        logger.error(f"Error updating speaker display name: {e}")


def update_speaker_profile(
    speaker_uuid: str,
    profile_id: int | None,
    profile_uuid: str | None,
    verified: bool = False,
    display_name: str | None = None,
):
    """
    Update the profile assignment of a speaker in OpenSearch

    Args:
        speaker_uuid: UUID of the speaker
        profile_id: Profile ID to assign (or None to clear, for internal queries)
        profile_uuid: Profile UUID to assign (or None to clear)
        verified: Whether the speaker is verified
        display_name: Optional display name to sync
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return

    try:
        # Update the speaker document with new profile assignment using UUID
        doc: dict = {
            "profile_id": profile_id,
            "profile_uuid": str(profile_uuid) if profile_uuid else None,
            "verified": verified,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if display_name is not None:
            doc["display_name"] = display_name
        update_body = {"doc": doc}

        response = opensearch_client.update(
            index=get_speaker_index(),
            id=str(speaker_uuid),
            body=update_body,
        )

        logger.info(
            f"Updated profile assignment for speaker {speaker_uuid} to profile {profile_uuid}, verified={verified}"
        )
        return response

    except Exception as e:
        logger.error(f"Error updating speaker profile assignment: {e}")


def sync_speaker_profiles_to_opensearch(db) -> dict:
    """Bulk-sync speaker profile_id, display_name, and verified from PostgreSQL to OpenSearch.

    Finds all speakers that have a profile_id or display_name in PostgreSQL and
    updates their corresponding OpenSearch documents. This repairs drift caused by
    profile assignments that bypassed the normal API update path.

    Returns:
        Dict with counts: updated, skipped, errors.
    """
    from app.models.media import Speaker

    if not opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping profile sync")
        return {"updated": 0, "skipped": 0, "errors": 0}

    speakers = (
        db.query(Speaker)
        .filter((Speaker.profile_id.isnot(None)) | (Speaker.display_name.isnot(None)))
        .all()
    )

    updated = 0
    skipped = 0
    errors = 0

    for speaker in speakers:
        try:
            profile_uuid = str(speaker.profile.uuid) if speaker.profile else None
            update_speaker_profile(
                speaker_uuid=str(speaker.uuid),
                profile_id=int(speaker.profile_id) if speaker.profile_id else None,
                profile_uuid=profile_uuid,
                verified=bool(speaker.verified) if hasattr(speaker, "verified") else False,
                display_name=str(speaker.display_name) if speaker.display_name else None,
            )
            updated += 1
        except Exception as e:
            if "document_missing_exception" in str(e):
                skipped += 1
            else:
                logger.warning(f"Error syncing speaker {speaker.uuid}: {e}")
                errors += 1

    logger.info(
        f"Speaker profile sync complete: {updated} updated, {skipped} skipped "
        f"(no OS doc), {errors} errors"
    )
    return {"updated": updated, "skipped": skipped, "errors": errors}


def find_matching_profiles(
    embedding: list[float],
    user_id: int,
    threshold: float = 0.7,
    size: int = 5,
    organization_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Find matching speaker profiles using embedding similarity in OpenSearch.

    Args:
        embedding: Query embedding vector
        user_id: User ID to filter results
        threshold: Minimum similarity threshold
        size: Maximum number of results
        organization_id: Active org id (None = personal) — tenant gate.

    Returns:
        List of matching profiles with similarity scores
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # KNN search query for profile embeddings
        query = {
            "size": size,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding,
                        "k": size,
                        "filter": {
                            "bool": {
                                "must": [
                                    {"term": {"user_id": user_id}},
                                    {"term": {"document_type": "profile"}},
                                    *_speaker_org_filter_clauses(organization_id),
                                ]
                            }
                        },
                    }
                }
            },
            "_source": ["profile_id", "profile_name", "embedding_count", "updated_at"],
        }

        response = opensearch_client.search(index=get_speaker_index(), body=query)

        matches = []
        for hit in response["hits"]["hits"]:
            score = 2.0 * hit["_score"] - 1.0  # raw cosine
            if score >= threshold:
                source = hit["_source"]
                matches.append(
                    {
                        "profile_id": source["profile_id"],
                        "profile_name": source["profile_name"],
                        "similarity": score,
                        "embedding_count": source["embedding_count"],
                        "last_update": source.get("updated_at"),
                    }
                )

        logger.info(f"Found {len(matches)} profile matches above threshold {threshold}")
        return matches

    except Exception as e:
        logger.error(f"Error finding matching profiles: {e}")
        return []


def update_cluster_embedding(
    cluster_uuid: str,
    embedding: list[float],
    label: str | None = None,
) -> bool:
    """Update an existing cluster centroid embedding in OpenSearch.

    Re-indexes the full document (same as store) so that the embedding vector
    is replaced atomically.

    Args:
        cluster_uuid: UUID of the cluster.
        embedding: Updated centroid embedding vector.
        label: Optional updated label for the cluster.

    Returns:
        True if successful, False otherwise.
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    try:
        ensure_indices_exist()
        active_index = get_active_speaker_index()

        # Fetch existing document to preserve user_id
        doc_id = f"cluster_{cluster_uuid}"
        try:
            existing = opensearch_client.get(index=active_index, id=doc_id)
            user_id = existing["_source"].get("user_id")
            existing_label = existing["_source"].get("label")
        except Exception:
            logger.error(f"Cannot update cluster {cluster_uuid}: document not found")
            return False

        doc = {
            "document_type": "cluster",
            "cluster_uuid": str(cluster_uuid),
            "user_id": user_id,
            "embedding": embedding,
            "label": label if label is not None else existing_label,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }

        opensearch_client.index(
            index=active_index,
            body=doc,
            id=doc_id,
            refresh="wait_for",
        )

        logger.info(f"Updated cluster {cluster_uuid} embedding in OpenSearch")
        return True

    except Exception as e:
        logger.error(f"Error updating cluster embedding for {cluster_uuid}: {e}")
        return False


def cleanup_orphaned_speaker_embeddings(user_id: int) -> int:
    """
    Remove speaker embeddings from OpenSearch for MediaFiles that no longer exist in PostgreSQL.

    Args:
        user_id: ID of the user to clean up orphaned documents for

    Returns:
        Number of orphaned documents removed
    """
    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return 0

    try:
        from app.db.session_utils import session_scope
        from app.models.media import MediaFile

        with session_scope() as db:
            # Get all existing MediaFile IDs for this user
            existing_media_file_ids = set(
                row[0] for row in db.query(MediaFile.id).filter(MediaFile.user_id == user_id).all()
            )
            logger.info(
                f"Found {len(existing_media_file_ids)} existing MediaFiles for user {user_id}: {existing_media_file_ids}"
            )

        # Query OpenSearch for all speaker documents for this user
        query = {
            "size": 1000,  # Adjust if needed
            "query": {
                "bool": {
                    "must": [
                        {"term": {"user_id": user_id}},
                        {
                            "bool": {"must_not": {"exists": {"field": "document_type"}}}
                        },  # Only speaker docs, not profiles
                    ]
                }
            },
            "_source": ["speaker_id", "speaker_uuid", "media_file_id"],
        }

        response = opensearch_client.search(index=get_speaker_index(), body=query)

        orphaned_speaker_uuids = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            media_file_id = source.get("media_file_id")
            speaker_id = source.get("speaker_id")
            speaker_uuid = source.get("speaker_uuid")

            if media_file_id and media_file_id not in existing_media_file_ids:
                orphaned_speaker_uuids.append(speaker_uuid)
                logger.info(
                    f"Found orphaned speaker {speaker_uuid} (ID: {speaker_id}) referencing non-existent MediaFile {media_file_id}"
                )

        # Delete orphaned documents using UUIDs
        deleted_count = 0
        for speaker_uuid in orphaned_speaker_uuids:
            try:
                opensearch_client.delete(index=get_speaker_index(), id=str(speaker_uuid))
                logger.info(f"Deleted orphaned speaker document for speaker {speaker_uuid}")
                deleted_count += 1
            except Exception as e:
                logger.error(f"Error deleting orphaned speaker {speaker_uuid}: {e}")

        logger.info(
            f"Cleanup completed: removed {deleted_count} orphaned speaker documents for user {user_id}"
        )
        return deleted_count

    except Exception as e:
        logger.error(f"Error during orphaned speaker cleanup: {e}")
        return 0
