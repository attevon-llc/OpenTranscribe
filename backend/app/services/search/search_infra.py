"""Per-process verification that the chunks index and a fusion pipeline exist.

The query path checks its infrastructure once per process (per pipeline id, see
below) rather than once per request; ``reset_infrastructure_state`` drops the cache
after an index is recreated.
"""

import logging
import threading
from typing import TYPE_CHECKING

from app.services.search.fusion import FusionConfig
from app.services.search.fusion import resolve_fusion
from app.services.search.fusion import search_pipeline_id
from app.services.search.index_admin import ensure_chunks_index_exists
from app.services.search.pipelines import ensure_search_pipeline_exists

if TYPE_CHECKING:  # pragma: no cover - import cost paid only by type checkers
    from opensearchpy import OpenSearch

logger = logging.getLogger(__name__)


# Module-level caches for index/pipeline existence checks.
#
# ``_verified_pipelines`` is a SET of pipeline ids, not a bool. Since #363 a
# request may name its own fusion strategy, and each strategy is a separate
# OpenSearch search pipeline — a single "the pipeline is verified" flag would
# have let the FIRST strategy a process saw certify every later one, so the
# second arm of an A/B would attach a pipeline id that had never been created
# and OpenSearch would run the query **unfused**. That is a plausible number,
# not an error. ``reset_infrastructure_state`` clears the set.
_index_verified = False
_verified_pipelines: set[str] = set()

# Lock for module-level state mutations
_state_lock = threading.Lock()


def ensure_fusion_pipeline(fusion: FusionConfig | None = None) -> str:
    """Ensure ``fusion``'s search pipeline exists, and return the id to attach.

    Verification is cached **per pipeline id** so a process that has served RRF
    still creates the normalization pipeline the first time one is asked for.
    Attaching a ``search_pipeline`` that does not exist is not an error in
    OpenSearch's eyes at the point the id is chosen — it fails at query time, or
    worse, an earlier arm's pipeline is already there and the run silently
    measures it.

    Args:
        fusion: The requested strategy, or None for the configured default.

    Returns:
        The pipeline id to pass as the ``search_pipeline`` request parameter.
    """
    cfg = resolve_fusion(fusion)
    pipeline_id = search_pipeline_id(cfg)
    if pipeline_id in _verified_pipelines:
        return pipeline_id
    with _state_lock:
        if pipeline_id not in _verified_pipelines:
            ensure_search_pipeline_exists(cfg)
            # Recorded even when creation failed, matching the pre-#363 flag: a
            # cluster that cannot hold a pipeline is not fixed by asking it once
            # per request, and search degrades to unfused rather than stalling.
            _verified_pipelines.add(pipeline_id)
    return pipeline_id


def _ensure_infrastructure(fusion: FusionConfig | None = None) -> str:
    """Ensure the OpenSearch index and this request's search pipeline exist.

    Args:
        fusion: The requested strategy, or None for the configured default.

    Returns:
        The pipeline id to attach to this request.
    """
    global _index_verified
    if not _index_verified:
        with _state_lock:
            if not _index_verified:
                ensure_chunks_index_exists()
                _index_verified = True
    return ensure_fusion_pipeline(fusion)


def reset_infrastructure_state() -> None:
    """Reset index/pipeline verification state. Call after index recreation.

    Clears **every** verified pipeline id, not just the default one. A stale id
    left behind here is exactly the "cached infrastructure state that survived a
    config change" failure this repo keeps hitting: the next request would
    attach a pipeline nobody re-checked.
    """
    global _index_verified
    with _state_lock:
        _index_verified = False
        _verified_pipelines.clear()
    logger.info("Infrastructure verification state reset")


def _chunks_index_verified(client: "OpenSearch", index_name: str) -> bool:
    """Whether the chunks index exists, remembered in ``_index_verified`` once it does.

    The read-only twin of :func:`_ensure_infrastructure`'s index check, shared by the
    autocomplete and filter-facet readers (``suggestions``): they never create the
    index, they answer empty without it, so a failed probe reads as "not there".
    """
    global _index_verified
    if not _index_verified:
        try:
            if not client.indices.exists(index=index_name):
                return False
            _index_verified = True
        except Exception:
            return False
    return True
