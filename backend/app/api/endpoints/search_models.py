"""Embedding and neural search model administration endpoints."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.core.config import settings
from app.core.constants import OPENSEARCH_EMBEDDING_MODELS
from app.models.user import User
from app.schemas.search import SetEmbeddingModelSchema
from app.services.request_context import RequestContext

logger = logging.getLogger(__name__)

router = APIRouter()


def _switch_model(model_name: str, triggered_by: int) -> dict[str, Any]:
    """Run the model switch and turn its two refusals into HTTP status codes.

    The switch itself is business logic and lives in
    ``services/search/model_switch.py``; this is the response-shaping half.
    ``409`` rather than ``400`` for an undeployed model because the request is
    well-formed — the *deployment* is not in a state that can honour it, and the
    remedy (register, then deploy) is named in the detail.

    ``503`` for a dispatch failure is the one that reports a **half-applied**
    switch: the settings, pipeline and index mapping have already changed, and
    only the re-embed failed to queue. It is an error rather than a partial
    success on purpose — the resulting index is exactly the mixed vector space
    #437 exists to prevent, and the detail says so.

    Args:
        model_name: A key of ``OPENSEARCH_EMBEDDING_MODELS``.
        triggered_by: User id performing the switch.

    Returns:
        The service's result dict.

    Raises:
        HTTPException: 400 unknown model, 409 model not registered *and*
            deployed, 503 the re-embed could not be queued.
    """
    from app.services.search.model_switch import EmbeddingModelNotDeployedError
    from app.services.search.model_switch import ReindexDispatchError
    from app.services.search.model_switch import UnknownEmbeddingModelError
    from app.services.search.model_switch import apply_embedding_model_switch

    try:
        return apply_embedding_model_switch(model_name, triggered_by)
    except UnknownEmbeddingModelError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except EmbeddingModelNotDeployedError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ReindexDispatchError as e:
        logger.error(f"Model switch left half-applied: {e}")
        raise HTTPException(status_code=503, detail=str(e)) from e


def _deployed_model_names() -> set[str]:
    """Names of every model OpenSearch can currently embed with.

    Feeds the ``ready`` flag on the picker. Without it the settings UI offered every
    model identically, and choosing one that had never been downloaded answered **409
    with instructions to POST two API endpoints by hand** — which is not a UI. The
    409 itself is correct and stays (#437): recording a selection whose pipeline
    cannot emit the new dimension makes the reindex coordinator delete the chunks
    index and then fail every write. What was missing was any way to see the
    condition coming, or to satisfy it.

    ONE cluster call for all models, not one per model: this runs on every settings
    page load. Failure returns the empty set, so an unreachable cluster renders every
    model as not-ready — the switch would refuse anyway, and claiming readiness we
    cannot confirm is the direction that ends in a deleted index.
    """
    try:
        from app.services.search.ml_model_service import get_ml_model_service

        return {
            str(model.get("name", ""))
            for model in get_ml_model_service().list_models(deployed_only=True)
        }
    except Exception:
        logger.warning("Could not resolve deployed models; reporting none as ready", exc_info=True)
        return set()


@router.get("/models")
def get_embedding_models(
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Get available embedding models for search indexing.

    Returns the list of sentence transformer models that can be used
    for semantic search embedding.

    ``languages`` and ``language_type`` are part of the payload because the settings UI
    is where a model is actually chosen, and without them a multilingual model was
    identifiable only by reading its display name. The ops endpoint has returned both
    all along; the two views disagreed for no reason.
    """
    from app.services.search.settings_service import get_search_embedding_model

    # Resolved ONCE, outside the comprehension: inside it this would be one cluster
    # round trip per model on every settings page load.
    deployed = _deployed_model_names()
    models = [
        {
            "model_id": model_name,
            "name": info["name"],
            "dimension": info["dimension"],
            "description": info["description"],
            "size_mb": info["size_mb"],
            "languages": info["languages"],
            "language_type": info["language_type"],
            "ready": model_name in deployed,
        }
        for model_name, info in OPENSEARCH_EMBEDDING_MODELS.items()
    ]

    return {
        "models": models,
        "current_model_id": get_search_embedding_model(),
    }


@router.post("/models")
def set_embedding_model(
    request: SetEmbeddingModelSchema,
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Set the embedding model and reindex every user's transcripts.

    The settings-UI entry point. It delegates to
    ``services/search/model_switch`` — it used to write the settings row and
    nothing else, which changed the recorded model and the index dimension while
    leaving the ingest pipeline embedding with the previous model (issue #437).
    """
    ctx.bypass.require_deployment_scope("Switching the embedding model")
    applied = _switch_model(request.model_id, current_user.id)
    model_info = OPENSEARCH_EMBEDDING_MODELS[request.model_id]
    return {
        **applied,
        "status": "model_changed",
        "model_id": applied["model_name"],
        "message": (
            f"Switched to {model_info['name']}. Re-indexing the transcripts of "
            f"{applied['reindex_users']} user(s)."
        ),
    }


@router.get("/models/neural")
def get_neural_models(
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Get available OpenSearch neural search models with deployment status.

    Returns both the model registry and current deployment state.

    Returns:
        Dict with models list, neural_enabled flag, and active model info.
    """
    from app.services.search.ml_model_service import get_ml_model_service

    ml_service = get_ml_model_service()

    # Get registered/deployed models from OpenSearch
    deployed_models = ml_service.list_models()
    deployed_by_name = {m["name"]: m for m in deployed_models}

    # Build model list from registry with deployment status
    models = []
    for model_name, info in OPENSEARCH_EMBEDDING_MODELS.items():
        deployed_info = deployed_by_name.get(model_name, {})
        models.append(
            {
                "model_name": model_name,
                "display_name": info["name"],
                "dimension": info["dimension"],
                "size_mb": info["size_mb"],
                "languages": info["languages"],
                "description": info["description"],
                "default": info.get("default", False),
                "registered": bool(deployed_info.get("model_id")),
                "deployed": deployed_info.get("deployed", False),
                "model_id": deployed_info.get("model_id"),
                "state": deployed_info.get("state", "NOT_REGISTERED"),
            }
        )

    # Get active model
    active_model_id = ml_service.get_active_model_id()
    active_model_name = None
    if active_model_id:
        for m in deployed_models:
            if m.get("model_id") == active_model_id:
                active_model_name = m.get("name")
                break

    return {
        "neural_enabled": settings.OPENSEARCH_NEURAL_SEARCH_ENABLED,
        "models": models,
        "active_model_id": active_model_id,
        "active_model_name": active_model_name,
    }


@router.post("/models/neural/{model_name:path}/register")
def register_neural_model(
    model_name: str,
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """
    Register a neural model in OpenSearch.

    Downloads and registers the model from HuggingFace. This may take
    several minutes depending on model size.

    Args:
        model_name: Full model name from OPENSEARCH_EMBEDDING_MODELS.

    Returns:
        Dict with registration status and model_id.
    """
    ctx.bypass.require_deployment_scope("Registering a neural model")
    if model_name not in OPENSEARCH_EMBEDDING_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model: {model_name}. Available: {list(OPENSEARCH_EMBEDDING_MODELS.keys())}",
        )

    from app.services.search.ml_model_service import get_ml_model_service

    ml_service = get_ml_model_service()

    # Check if already registered
    existing_id = ml_service.find_model_by_name(model_name)
    if existing_id:
        return {
            "status": "already_registered",
            "model_id": existing_id,
            "model_name": model_name,
        }

    # Register the model
    model_info = OPENSEARCH_EMBEDDING_MODELS[model_name]
    model_id = ml_service.register_model(
        model_name=model_name,
        model_format=str(model_info.get("model_format", "TORCH_SCRIPT")),
        description=str(model_info.get("description", "")),
    )

    if not model_id:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to register model {model_name}. Check OpenSearch logs.",
        )

    logger.info(f"Registered neural model {model_name} as {model_id}")

    return {
        "status": "registered",
        "model_id": model_id,
        "model_name": model_name,
    }


@router.post("/models/neural/{model_name:path}/deploy")
def deploy_neural_model(
    model_name: str,
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """
    Deploy a registered neural model for inference.

    The model must be registered first. Deployment loads the model into
    memory for fast inference.

    Args:
        model_name: Full model name from OPENSEARCH_EMBEDDING_MODELS.

    Returns:
        Dict with deployment status.
    """
    ctx.bypass.require_deployment_scope("Deploying a neural model")
    from app.services.search.ml_model_service import get_ml_model_service

    ml_service = get_ml_model_service()

    # Find the model
    model_id = ml_service.find_model_by_name(model_name)
    if not model_id:
        raise HTTPException(
            status_code=404,
            detail=f"Model {model_name} not registered. Register it first.",
        )

    # Check if already deployed
    status = ml_service.get_model_status(model_id)
    if status.get("deployed"):
        return {
            "status": "already_deployed",
            "model_id": model_id,
            "model_name": model_name,
        }

    # Deploy the model
    if not ml_service.deploy_model(model_id):
        raise HTTPException(
            status_code=500,
            detail=f"Failed to deploy model {model_name}. Check OpenSearch logs.",
        )

    logger.info(f"Deployed neural model {model_name} ({model_id})")

    return {
        "status": "deployed",
        "model_id": model_id,
        "model_name": model_name,
    }


@router.post("/models/neural/{model_name:path}/undeploy")
def undeploy_neural_model(
    model_name: str,
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """
    Undeploy a neural model to free memory.

    The model remains registered and can be redeployed later.

    Args:
        model_name: Full model name from OPENSEARCH_EMBEDDING_MODELS.

    Returns:
        Dict with undeploy status.
    """
    ctx.bypass.require_deployment_scope("Undeploying a neural model")
    from app.services.search.ml_model_service import get_ml_model_service

    ml_service = get_ml_model_service()

    # Find the model
    model_id = ml_service.find_model_by_name(model_name)
    if not model_id:
        raise HTTPException(
            status_code=404,
            detail=f"Model {model_name} not registered.",
        )

    if not ml_service.undeploy_model(model_id):
        raise HTTPException(
            status_code=500,
            detail=f"Failed to undeploy model {model_name}.",
        )

    # Reset neural search state since active model may have changed
    from app.services.search.hybrid_search_service import reset_neural_search_state

    reset_neural_search_state()

    logger.info(f"Undeployed neural model {model_name} ({model_id})")

    return {
        "status": "undeployed",
        "model_id": model_id,
        "model_name": model_name,
    }


@router.put("/models/neural/active")
def set_active_neural_model(
    model_name: str = Query(..., description="Model name to set as active"),
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Set the active neural model for search and reindex every user's transcripts.

    The model must be registered and deployed first. This will:

    1. Persist the model **and its dimension** to settings
    2. Update the neural ingest pipeline with the new model
    3. Recreate the index if the dimension changed
    4. Dispatch a reindex for **every** user that owns transcripts
    5. Reset search caches

    Step 1 used to be missing (issue #437), so the reindex coordinator then read
    the *previous* dimension and recreated the index a second time at the wrong
    size; step 4 used to cover only the caller, leaving every other user's chunks
    in the old model's vector space. Both live in
    ``services/search/model_switch.apply_embedding_model_switch``, which
    ``POST /search/models`` shares — the two endpoints were each doing a different
    half of the same job.

    Args:
        model_name: Full model name from OPENSEARCH_EMBEDDING_MODELS.

    Returns:
        Dict with status, reindex task ids, and the post-switch provenance survey.
    """
    ctx.bypass.require_deployment_scope("Switching the active neural model")
    applied = _switch_model(model_name, current_user.id)
    model_info = OPENSEARCH_EMBEDDING_MODELS[model_name]
    return {
        **applied,
        "status": "active_model_set",
        "message": (
            f"Switched to {model_info['name']}. Re-indexing the transcripts of "
            f"{applied['reindex_users']} user(s) with the neural pipeline."
        ),
    }


@router.get("/models/neural/status")
def get_neural_search_status(
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Get the current neural search status.

    Returns:
        Dict with neural search enabled flag, active model, pipeline status, and
        the ``embedding_provenance`` survey — the one query that answers whether
        the index is a single comparable vector space (issue #437). It lives here
        rather than in ``POST /search/repair-indices`` because that endpoint's
        machinery is close/reopen/force-merge, which cannot repair a mixed vector
        space; reporting it there would imply the repair had addressed it. Only a
        reindex does, and this is the endpoint an operator reads before deciding
        to run one.

        Also carries ``chunks_index_knn`` — a real kNN query against the index,
        not a configuration read. Every other field here can report perfect
        health while the vector segments are corrupt and *every* semantic query
        answers 503, because they describe the pipeline, the model registry and a
        ``terms`` aggregation, none of which touch the HNSW graph (issue #540).

        And ``bootstrap`` — the self-heal's own state (issue #625): whether the beat task
        is currently degraded, its attempt count, last error and next retry time, plus a
        report-only ``text_only_chunk_files`` count (no auto re-embed; see #626).
    """
    from app.services.opensearch_service import probe_knn_health_cached
    from app.services.search.embedding_provenance import survey_embedding_models
    from app.services.search.ml_model_service import get_ml_model_service
    from app.services.search.model_switch import provenance_payload
    from app.services.search.neural_bootstrap import bootstrap_status
    from app.services.search.pipelines import is_neural_pipeline_available

    ml_service = get_ml_model_service()
    active_model_id = ml_service.get_active_model_id()

    # Get active model details
    active_model_name = None
    active_model_info = None
    if active_model_id:
        status = ml_service.get_model_status(active_model_id)
        active_model_name = status.get("name")
        if active_model_name and active_model_name in OPENSEARCH_EMBEDDING_MODELS:
            active_model_info = OPENSEARCH_EMBEDDING_MODELS[active_model_name]

    knn_probe = probe_knn_health_cached(settings.OPENSEARCH_CHUNKS_INDEX)

    return {
        "neural_enabled": settings.OPENSEARCH_NEURAL_SEARCH_ENABLED,
        "neural_pipeline_available": is_neural_pipeline_available(),
        "active_model_id": active_model_id,
        "active_model_name": active_model_name,
        "active_model_dimension": active_model_info["dimension"] if active_model_info else None,
        "pipeline_name": settings.OPENSEARCH_NEURAL_PIPELINE,
        "embedding_provenance": provenance_payload(survey_embedding_models()),
        "bootstrap": bootstrap_status(),
        "chunks_index_knn": {
            "index": settings.OPENSEARCH_CHUNKS_INDEX,
            "status": knn_probe.status,
            "healthy": knn_probe.is_serviceable,
            "detail": knn_probe.detail,
            "latency_ms": knn_probe.latency_ms,
        },
    }
