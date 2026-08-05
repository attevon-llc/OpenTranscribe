"""Speaker index alias management and active-index resolution.

The ``speakers`` alias points at a concrete versioned index (``speakers_v3``
512d or ``speakers_v4`` 256d). This module owns the alias migration, the alias
swap, and the cached lookup of whichever concrete index is currently active.
"""

import contextlib
import logging
import time
from typing import Any

from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v3
from app.core.constants import get_speaker_index_v4
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.client import _get_alias_target
from app.services.opensearch_service.client import _get_index_embedding_dimension
from app.services.opensearch_service.client import _is_alias
from app.services.opensearch_service.client import _safe_index_exists

logger = logging.getLogger(__name__)


# Cache for get_active_speaker_index() — avoids hundreds of OpenSearch
# round-trips during batch re-clustering (#17).
_active_index_cache: tuple[str, float] | None = None
_ACTIVE_INDEX_CACHE_TTL = 30.0  # seconds


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

    if not _client.opensearch_client:
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
        concrete_exists = _client.opensearch_client.indices.exists(index=alias_name)

    if not concrete_exists:
        # Fresh install OR the index was deleted. Check for versioned indices.
        v3_exists = _safe_index_exists(v3_index)
        v4_exists = _safe_index_exists(v4_index)
        backup_exists = _safe_index_exists(v3_backup)

        if v3_exists or v4_exists:
            # Versioned indices exist but no alias — create alias to whichever exists
            target = v4_index if v4_exists else v3_index
            _client.opensearch_client.indices.put_alias(index=target, name=alias_name)
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
        doc_count = _client.opensearch_client.count(index=alias_name)["count"]

    if dimension == PYANNOTE_EMBEDDING_DIMENSION_V3 or dimension == 512:
        target_index = v3_index
        mode_label = "v3"
    elif dimension == PYANNOTE_EMBEDDING_DIMENSION_V4 or dimension == 256:
        target_index = v4_index
        mode_label = "v4"
    elif doc_count == 0:
        # Empty index with unknown dimension — delete and let ensure_indices_exist handle it
        _client.opensearch_client.indices.delete(index=alias_name)
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
            target_count = _client.opensearch_client.count(index=target_index)["count"]

        if target_count >= doc_count:
            # Versioned index has more/equal data — just delete concrete and alias
            _client.opensearch_client.indices.delete(index=alias_name)
            _client.opensearch_client.indices.put_alias(index=target_index, name=alias_name)
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
            _client.opensearch_client.indices.delete(index=target_index)
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


def _reindex_and_alias(source_index: str, target_index: str, alias_name: str) -> None:
    """Reindex source → target, delete source, create alias on target.

    This is used to "rename" a concrete index since OpenSearch has no
    native rename operation.
    """
    if not _client.opensearch_client:
        return

    # Copy the mapping from source to create target with correct settings
    try:
        source_mapping = _client.opensearch_client.indices.get(index=source_index)
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

        _client.opensearch_client.indices.create(index=target_index, body=target_config)
    except Exception as e:
        logger.warning(f"Could not create target from source mapping: {e}. Trying plain reindex.")

    # Reindex data
    _client.opensearch_client.reindex(
        body={"source": {"index": source_index}, "dest": {"index": target_index}},
        wait_for_completion=True,
    )

    # Verify
    source_count = _client.opensearch_client.count(index=source_index)["count"]
    target_count = _client.opensearch_client.count(index=target_index)["count"]
    if target_count < source_count * 0.95:
        raise RuntimeError(
            f"Reindex verification failed: {source_index} has {source_count} docs "
            f"but {target_index} only has {target_count}"
        )

    # Delete source and create alias
    _client.opensearch_client.indices.delete(index=source_index)
    _client.opensearch_client.indices.put_alias(index=target_index, name=alias_name)

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
    if not _client.opensearch_client:
        return {"status": "error", "reason": "no_client"}

    alias_name = get_speaker_index()
    old_target = _get_alias_target(alias_name)

    actions: list[dict] = []

    # Remove alias from old target (if any)
    if old_target:
        actions.append({"remove": {"index": old_target, "alias": alias_name}})

    # Add alias to new target
    actions.append({"add": {"index": new_target, "alias": alias_name}})

    _client.opensearch_client.indices.update_aliases(body={"actions": actions})
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

    if not _client.opensearch_client:
        return main_index

    result = main_index
    try:
        # If alias is set up, check if v4 staging has more data (pre-finalization)
        main_count = 0
        v4_count = 0

        # Count through alias (resolves to active versioned index)
        with contextlib.suppress(Exception):
            main_count = _client.opensearch_client.count(index=main_index)["count"]

        if _safe_index_exists(v4_index):
            # Only check v4 if it's NOT the alias target (i.e., during pre-finalization)
            alias_target = _get_alias_target(main_index)
            if alias_target != v4_index:
                with contextlib.suppress(Exception):
                    v4_count = _client.opensearch_client.count(index=v4_index)["count"]

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
