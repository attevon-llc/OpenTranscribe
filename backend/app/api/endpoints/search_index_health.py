"""Search index health and repair endpoints."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_admin_user
from app.core.config import settings
from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v4
from app.db.base import get_db
from app.models.user import User
from app.services.request_context import RequestContext

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/index-health")
def get_index_health(
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """Return health status of each OpenSearch index.

    Tests each index with a simple match_all query (size=0) and
    returns per-index status with doc counts. Admins get instance-wide counts;
    any other user gets the count of their OWN documents in the active tenant.

    Returns:
        Dict with per-index health: {index_name: {status, doc_count, error}}.
    """
    from app.services.opensearch_service import opensearch_client

    current_user = ctx.user
    indices = [
        get_speaker_index(),
        settings.OPENSEARCH_TRANSCRIPT_INDEX,
        get_speaker_index_v4(),
        settings.OPENSEARCH_CHUNKS_INDEX,
    ]

    health: dict[str, Any] = {}

    if not opensearch_client:
        for idx in indices:
            health[idx] = {
                "status": "red",
                "doc_count": 0,
                "error": "OpenSearch client not initialized",
            }
        return health

    # Resolve aliases to concrete index names, then use a single _cat/indices
    # call to get doc counts for everything at once (replaces 8 sequential
    # HTTP calls with 1-2).
    alias_map: dict[str, str] = {}  # alias → concrete index name
    concrete_names: list[str] = []
    try:
        aliases_response = opensearch_client.cat.aliases(format="json", h="alias,index")
        for row in aliases_response:
            alias_map[row.get("alias", "")] = row.get("index", "")
    except Exception as e:
        logger.debug("Could not resolve aliases: %s", e)

    for idx in indices:
        concrete_names.append(alias_map.get(idx, idx))

    index_stats: dict[str, int] = {}
    try:
        cat_response = opensearch_client.cat.indices(
            index=",".join(concrete_names),
            format="json",
            h="index,docs.count",
        )
        for row in cat_response:
            idx_name = row.get("index", "")
            doc_count_str = row.get("docs.count", "0")
            index_stats[idx_name] = int(doc_count_str) if doc_count_str else 0
    except Exception as e:
        logger.debug("Bulk _cat/indices call failed, will mark missing: %s", e)

    for idx in indices:
        concrete = alias_map.get(idx, idx)
        if concrete in index_stats:
            health[idx] = {
                "status": "green",
                "doc_count": index_stats[concrete],
                "error": None,
            }
        else:
            health[idx] = {
                "status": "red",
                "doc_count": 0,
                "error": "Index does not exist or is unreachable",
            }

    if not current_user.is_admin:
        _scope_doc_counts_to_caller(opensearch_client, health, ctx)
    return health


def _scope_doc_counts_to_caller(client: Any, health: dict[str, Any], ctx: RequestContext) -> None:
    """Replace instance-wide doc counts with the caller's own, in the active tenant."""
    from app.services.search.tenant_scope import org_filter_clauses

    query = {
        "query": {
            "bool": {
                "filter": [{"term": {"user_id": ctx.user.id}}, *org_filter_clauses(ctx.org_id)]
            }
        }
    }
    for idx, entry in health.items():
        if entry["status"] != "green":
            entry["doc_count"] = 0
            continue
        try:
            entry["doc_count"] = int(client.count(index=idx, body=query).get("count", 0))
        except Exception as e:
            logger.debug("Scoped doc count failed for %s: %s", idx, e)
            entry["doc_count"] = 0


def _probe_index_health(indices: list[str]) -> dict[str, str]:
    """Classify each index as ``healthy`` / ``missing`` / ``unhealthy``.

    Two OpenSearch round trips **per index**, so it runs with **no DB session
    held** — hence a plain list in, a plain dict out, and no ``db`` parameter.
    An exception from either probe means "unhealthy": that is the repair trigger,
    and an ``exists`` call that raises is exactly as broken as a failing search.
    """
    from app.services.opensearch_service import opensearch_client

    health: dict[str, str] = {}
    for idx in indices:
        try:
            if not opensearch_client.indices.exists(index=idx):
                health[idx] = "missing"
                continue
            # Test if index is healthy
            opensearch_client.search(index=idx, body={"query": {"match_all": {}}, "size": 0})
            health[idx] = "healthy"
        except Exception:
            health[idx] = "unhealthy"
    return health


@router.post("/repair-indices")
def repair_indices(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Repair corrupted OpenSearch indices.

    For the speakers index (kNN), rebuilds from PostgreSQL data.
    For other indices, attempts close/reopen and force merge strategies.

    **Ordered so the request transaction is released across the OpenSearch work.**
    ``db`` comes from ``Depends(get_db)`` and lives for the whole request, so the
    eight probe round trips below used to run with a Postgres transaction open
    (opened by the admin-auth dependency) — ACCESS SHARE held for the length of a
    possibly-unreachable cluster's timeouts. The probes and the non-speaker
    repairs now run with nothing held; only ``rebuild_speaker_index`` needs
    Postgres, and it is left until last so it opens a fresh transaction of its own.

    Returns:
        Dict with per-index repair results.
    """
    ctx.bypass.require_deployment_scope("Repairing the search indices")
    from app.services.opensearch_service import _repair_index
    from app.services.opensearch_service import opensearch_client
    from app.services.opensearch_service import rebuild_speaker_index

    if not opensearch_client:
        raise HTTPException(status_code=503, detail="OpenSearch client not available")

    speaker_index = get_speaker_index()
    indices = [
        speaker_index,
        settings.OPENSEARCH_TRANSCRIPT_INDEX,
        get_speaker_index_v4(),
        settings.OPENSEARCH_CHUNKS_INDEX,
    ]

    # Release the request transaction before the OpenSearch phase below.
    db.close()

    # Phase 1 — probe every index, with no Postgres transaction held.
    health = _probe_index_health(indices)
    results: dict[str, str] = {idx: state for idx, state in health.items() if state != "unhealthy"}
    speakers_indexed = 0

    # Phase 2 — repair the non-speaker indices. Still OpenSearch-only.
    for idx in indices:
        if health[idx] != "unhealthy" or idx == speaker_index:
            continue
        repaired = _repair_index(idx)
        results[idx] = "repaired" if repaired else "failed"

    # Phase 3 — the one repair that reads Postgres, in a transaction of its own.
    if health.get(speaker_index) == "unhealthy":
        try:
            rebuild_result = rebuild_speaker_index(db)
            if rebuild_result.get("status") == "rebuilt":
                results[speaker_index] = "rebuilt"
                speakers_indexed = rebuild_result.get("speakers_indexed", 0)
            else:
                results[speaker_index] = "failed"
        except Exception as e:
            logger.exception(f"Failed to rebuild speakers index: {e}")
            results[speaker_index] = "failed"

    any_failed = any(v == "failed" for v in results.values())

    return {
        "status": "partial_failure" if any_failed else "success",
        "indices": results,
        "speakers_indexed": speakers_indexed,
        "message": (
            "Some indices could not be repaired." if any_failed else "Index repair complete."
        ),
    }
