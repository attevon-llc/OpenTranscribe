"""Speaker index alias management and active-index resolution.

The ``speakers`` alias points at a concrete versioned index (``speakers_v3``
512d or ``speakers_v4`` 256d). This module owns the alias migration, the alias
swap, and the cached lookup of whichever concrete index is currently active.
"""

import logging
import time
from typing import Any

from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v3
from app.core.constants import get_speaker_index_v4
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.client import CLUSTER_UNAVAILABLE_ERRORS
from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.opensearch_service.client import _get_alias_target
from app.services.opensearch_service.client import _get_index_embedding_dimension
from app.services.opensearch_service.client import _is_alias
from app.services.opensearch_service.client import _safe_index_exists

logger = logging.getLogger(__name__)


# Cache for get_active_speaker_index() — avoids hundreds of OpenSearch
# round-trips during batch re-clustering (#17).
_active_index_cache: tuple[str, float] | None = None
_ACTIVE_INDEX_CACHE_TTL = 30.0  # seconds


def _count_docs(index_name: str) -> int | None:
    """Count documents in an index.

    Args:
        index_name: Index or alias to count.

    Returns:
        The document count, or **None** when the count could not be obtained.
        None is deliberately distinct from 0: callers use a zero count to
        authorise deleting an index, and an unanswered count must never be
        read as "empty".
    """
    if not _client.opensearch_client:
        return None
    try:
        return int(_client.opensearch_client.count(index=index_name)["count"])
    except CLUSTER_UNAVAILABLE_ERRORS as e:
        logger.warning(f"Could not count docs in '{index_name}': {e}")
        return None


def get_active_versioned_index() -> str:
    """Get the concrete versioned index name that the 'speakers' alias points to.

    Returns the alias target (e.g. 'speakers_v3' or 'speakers_v4'), or
    falls back to get_speaker_index() if aliases haven't been set up yet or
    OpenSearch is unreachable.
    """
    alias_name = get_speaker_index()
    try:
        target = _get_alias_target(alias_name)
    except OpenSearchUnavailableError as e:
        # Same fallback as "alias not set up", but no longer silent: callers
        # index and search against the returned name, so a wrong answer here
        # is worth an operator-visible log line.
        logger.error(f"Could not resolve the speaker alias, falling back to '{alias_name}': {e}")
        return alias_name
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
    if not _client.opensearch_client:
        return {"status": "skipped", "reason": "no_client"}

    try:
        return _migrate_to_alias_based_indices()
    except OpenSearchUnavailableError as e:
        # Fail CLOSED. Every branch of the migration decides whether to create,
        # alias, reindex or DELETE a live index, and it decides from index-
        # existence and document-count answers. When the cluster cannot answer,
        # those used to read as "nothing is there" — which routes straight into
        # the delete branch. Aborting is always the safe move: the migration is
        # idempotent and re-runs on the next startup.
        logger.error(f"Speaker alias migration aborted — OpenSearch unavailable: {e}")
        return {"status": "error", "reason": "opensearch_unavailable"}


def _migrate_to_alias_based_indices() -> dict[str, Any]:
    """Body of :func:`migrate_to_alias_based_indices`; see it for semantics."""
    from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3
    from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
    from app.core.constants import get_speaker_index_v3_backup

    assert _client.opensearch_client is not None  # noqa: S101 - checked by caller

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
    if not _safe_index_exists(alias_name):
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
    doc_count = _count_docs(alias_name)

    if dimension == PYANNOTE_EMBEDDING_DIMENSION_V3 or dimension == 512:
        target_index = v3_index
        mode_label = "v3"
    elif dimension == PYANNOTE_EMBEDDING_DIMENSION_V4 or dimension == 256:
        target_index = v4_index
        mode_label = "v4"
    elif doc_count == 0:
        # Confirmed-empty index with unknown dimension — delete and let
        # ensure_indices_exist recreate it. `doc_count is None` (count did not
        # answer) must NOT reach here; see the guard below.
        _client.opensearch_client.indices.delete(index=alias_name)
        logger.info(f"Deleted empty concrete '{alias_name}' index (no dimension detected)")
        return {"status": "deleted_empty"}
    elif doc_count is None:
        # Unknown dimension AND unknown doc count: refuse to guess.
        logger.error(
            f"Refusing to migrate '{alias_name}': its embedding dimension and document "
            "count are both unknown. Retrying on the next startup."
        )
        return {"status": "error", "reason": "indeterminate_index_state"}
    else:
        # Unknown dimension with data — default to v3 to be safe
        target_index = v3_index
        mode_label = "v3 (fallback)"

    if doc_count is None:
        logger.error(
            f"Refusing to migrate '{alias_name}' → '{target_index}': its document count "
            "is unknown, so the merge direction cannot be decided safely."
        )
        return {"status": "error", "reason": "indeterminate_doc_count"}

    # Check if the target versioned index already exists
    if _safe_index_exists(target_index):
        # Both concrete 'speakers' and the versioned index exist. Whichever has
        # fewer docs gets deleted, so an unanswered count cannot be defaulted —
        # either default destroys the wrong copy. Abort instead.
        target_count = _count_docs(target_index)
        if target_count is None:
            logger.error(
                f"Refusing to reconcile '{alias_name}' and '{target_index}': the document "
                f"count for '{target_index}' is unknown, and the loser is deleted."
            )
            return {"status": "error", "reason": "indeterminate_doc_count"}

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
    except (*CLUSTER_UNAVAILABLE_ERRORS, KeyError) as e:
        # Best-effort: the target may already exist, or the source mapping may be
        # unreadable. Falling through to a plain reindex is correct — and if the
        # cluster is genuinely down, the unguarded reindex below raises anyway,
        # before anything is deleted.
        logger.warning(f"Could not create target from source mapping: {e}. Trying plain reindex.")

    # Reindex data
    _client.opensearch_client.reindex(
        body={"source": {"index": source_index}, "dest": {"index": target_index}},
        wait_for_completion=True,
    )

    # Verify BEFORE deleting the source. The bar is every document, not 95% of
    # them: the next statement destroys the only other copy, so a 5% tolerance
    # was standing permission to silently lose one voiceprint in twenty — and at
    # 50k embeddings that is 2,500 speakers whose identity cannot be recovered.
    # An equal count is the pass; a larger target (a partially pre-populated
    # index) is not this function's business to judge.
    source_count = _client.opensearch_client.count(index=source_index)["count"]
    target_count = _client.opensearch_client.count(index=target_index)["count"]
    if target_count < source_count:
        raise RuntimeError(
            f"Reindex verification failed: {source_index} has {source_count} docs "
            f"but {target_index} only has {target_count}. Leaving '{source_index}' "
            "in place — the next startup retries the migration."
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
        # Count through alias (resolves to active versioned index)
        main_count = _count_docs(main_index) or 0

        if _safe_index_exists(v4_index):
            # Only check v4 if it's NOT the alias target (i.e., during pre-finalization)
            alias_target = _get_alias_target(main_index)
            if alias_target != v4_index:
                v4_count = _count_docs(v4_index) or 0

                if v4_count > main_count:
                    logger.debug(
                        "Using v4 index '%s' (%d docs > %d in main)",
                        v4_index,
                        v4_count,
                        main_count,
                    )
                    result = v4_index

    except OpenSearchUnavailableError as e:
        # Read path: keeping the alias is the correct degrade (it is what the
        # v4 staging check exists to *refine*), but do not cache the guess.
        logger.warning(f"Could not detect the active speaker index, using '{main_index}': {e}")
        return main_index

    _active_index_cache = (result, now)
    return result


def invalidate_active_speaker_index_cache() -> None:
    """Force the next call to get_active_speaker_index() to re-query OpenSearch."""
    global _active_index_cache
    _active_index_cache = None
