"""Idempotent neural-search bootstrap, with a cheap probe and a self-heal tick (#625).

**The root cause.** ``app/main.py``'s old ``_initialize_neural_search`` fired exactly once
from ``lifespan`` via ``asyncio.create_task``, and every failure arm was a bare ``return``
after a ``logger.warning``. Nothing ever re-entered it. ``ml_model_service``'s registration
ceiling (``_REGISTRATION_MAX_WAIT``, 300s) is a poll ceiling on OpenSearch's own async
registration task, which keeps running server-side after we stop looking — so a model can
finish ``REGISTERED, deployed=False`` minutes after the API process gave up, and stay there
forever on a cold or slow OpenSearch boot. Consequence: the ingest pipeline never gets
created, and every file indexed during that window is written with ``use_neural=False`` /
``embedding_model: None`` — permanently, since ``search_index_maintenance`` only finds files
with NO chunks, not files with text-only chunks.

**The fix is one idempotent function with two callers**, not two implementations:

- ``app/main.py``'s startup fast path calls :func:`ensure_neural_search_bootstrap` once, same
  as before, but is no longer the only attempt.
- ``app/tasks/search_maintenance_task.neural_search_bootstrap_task`` calls the SAME function
  every 10 minutes via Celery beat. A healthy deployment pays only the cheap probe
  (:func:`neural_search_ready`) on every tick, forever — see
  ``backend/app/services/search/CLAUDE.md``'s "The bootstrap self-heals" section for why this
  is a beat task and not, say, a longer startup ceiling.

This module owns the whole sequence that used to be inlined in ``_initialize_neural_search``
(managed-mode adoption, ML settings, local-model scan, download, ``ensure_model_deployed``,
``set_active_model_id``, ``ensure_neural_ingest_pipeline``). It is a SEQUENCER, not a state
machine — ``ensure_model_deployed`` and ``ensure_neural_ingest_pipeline`` are already fully
idempotent/resumable, so this module does not reimplement any of that.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.core.constants import NEURAL_BOOTSTRAP_ALERT_AFTER_ATTEMPTS
from app.core.constants import NEURAL_BOOTSTRAP_BASE_BACKOFF_SECONDS
from app.core.constants import NEURAL_BOOTSTRAP_MAX_BACKOFF_SECONDS

logger = logging.getLogger(__name__)

#: TTL on the Redis attempt-bookkeeping keys. Generous relative to the backoff ceiling so a
#: long stretch of failures never has its own counter expire mid-sequence.
_ATTEMPT_KEY_TTL_SECONDS = 24 * 60 * 60

_ATTEMPTS_KEY = "neural_bootstrap:attempts"
_NEXT_AT_KEY = "neural_bootstrap:next_at"
_LAST_ERROR_KEY = "neural_bootstrap:last_error"


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of one :func:`ensure_neural_search_bootstrap` call."""

    state: str  # "ok" | "disabled" | "degraded"
    stage: str | None  # "ml_settings" | "register_deploy" | "pipeline" | None
    detail: str | None  # human-readable failure cause
    model_id: str | None


def _managed_embedding_mode() -> bool:
    """Whether the embedding model is owned by the OpenSearch domain, not by us."""
    return settings.OPENSEARCH_EMBEDDING_MODE.strip().lower() == "managed"


def neural_search_ready() -> bool:
    """Cheap derived check: is there an active model AND a verified pipeline?

    No OpenSearch mutation, no registration/deployment call — just the two reads
    :func:`ensure_neural_search_bootstrap` would otherwise have to do expensive work to
    establish. Safe to call on every beat tick forever.
    """
    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        return True  # disabled is not degraded

    from app.services.search.indexing_service import is_neural_pipeline_available
    from app.services.search.ml_model_service import get_ml_model_service

    ml_service = get_ml_model_service()
    return bool(ml_service.get_active_model_id()) and is_neural_pipeline_available()


def _adopt_managed_embedding_model(ml_service: Any) -> BootstrapResult:
    """Use an embedding model the OpenSearch domain already hosts (issue #284 A1.13).

    Moved here verbatim from ``app/main.py::_adopt_managed_embedding_model`` — see that
    docstring's original rationale, preserved below.

    The default "local" path mutates ML Commons cluster settings and registers a model from
    a ``file://`` or public ``https://`` URL. Amazon OpenSearch Service exposes neither knob,
    so on a managed domain that path fails at the first cluster-settings PUT and neural
    search never comes up. In "managed" mode the operator has already registered the model
    (typically a remote-model connector), and all we do is adopt its id and wire the ingest
    pipeline.
    """
    from app.services.search.indexing_service import ensure_neural_ingest_pipeline

    model_id = settings.OPENSEARCH_NEURAL_MODEL_ID.strip() or ml_service.get_active_model_id()
    if not model_id:
        detail = (
            "OPENSEARCH_EMBEDDING_MODE=managed but no model id is configured. Set "
            "OPENSEARCH_NEURAL_MODEL_ID to a model already registered in the domain, or set "
            "OPENSEARCH_NEURAL_SEARCH_ENABLED=false to run keyword-only search."
        )
        logger.warning(detail)
        return BootstrapResult(state="degraded", stage="ml_settings", detail=detail, model_id=None)

    ml_service.set_active_model_id(model_id)
    logger.info(f"Neural search using domain-managed model: {model_id}")

    if ensure_neural_ingest_pipeline(model_id):
        logger.info("Neural ingest pipeline configured successfully")
        return BootstrapResult(state="ok", stage=None, detail=None, model_id=model_id)

    detail = "Could not configure neural ingest pipeline"
    logger.warning(detail)
    return BootstrapResult(state="degraded", stage="pipeline", detail=detail, model_id=model_id)


def _bootstrap_local_mode(ml_service: Any) -> BootstrapResult:
    """The non-managed path: cluster settings, local/downloaded model, deploy, pipeline."""
    from app.services.search.indexing_service import ensure_neural_ingest_pipeline

    if not ml_service.configure_ml_settings():
        detail = "Could not configure ML Commons settings"
        logger.warning(detail)
        return BootstrapResult(state="degraded", stage="ml_settings", detail=detail, model_id=None)

    # Check for available local models (offline deployment support)
    local_models = ml_service.get_available_local_models()
    if local_models:
        model_names = [m["short_name"] for m in local_models]
        logger.info(f"Found {len(local_models)} local models for offline use: {model_names}")
    else:
        logger.warning("No local models found - attempting automatic download")

        from app.services.search.model_downloader import check_internet_connectivity
        from app.services.search.model_downloader import ensure_model_downloaded

        default_model = settings.OPENSEARCH_NEURAL_MODEL

        if check_internet_connectivity():
            logger.info(f"Internet available - downloading default model: {default_model}")
            model_path = ensure_model_downloaded(default_model)

            if model_path:
                logger.info(f"Model downloaded successfully: {model_path}")
                local_models = ml_service.get_available_local_models()
                if local_models:
                    logger.info("Models now available for offline use")
            else:
                logger.warning("Model download failed - will use remote registration")
        else:
            logger.warning("No internet connection - cannot download models")
            logger.warning("Will use remote registration (requires OpenSearch to download)")

    active_model_id = ml_service.get_active_model_id()

    if active_model_id:
        logger.info(f"Neural search already has active model: {active_model_id}")
    else:
        default_model = settings.OPENSEARCH_NEURAL_MODEL
        logger.info(f"No active model, attempting to setup default: {default_model}")

        local_path = ml_service.get_local_model_path(default_model)
        if local_path:
            logger.info(f"Default model available locally: {local_path}")
        else:
            logger.info("Default model not found locally, will download from remote")

        model_id = ml_service.ensure_model_deployed(default_model)
        if model_id:
            ml_service.set_active_model_id(model_id)
            active_model_id = model_id
            logger.info(f"Default neural model deployed: {default_model} -> {model_id}")
        else:
            detail = f"Could not deploy default model {default_model}"
            logger.warning(detail)
            return BootstrapResult(
                state="degraded", stage="register_deploy", detail=detail, model_id=None
            )

    if ensure_neural_ingest_pipeline():
        logger.info("Neural ingest pipeline configured successfully")
        return BootstrapResult(state="ok", stage=None, detail=None, model_id=active_model_id)

    detail = "Could not configure neural ingest pipeline"
    logger.warning(detail)
    return BootstrapResult(
        state="degraded", stage="pipeline", detail=detail, model_id=active_model_id
    )


def ensure_neural_search_bootstrap(*, force: bool = False) -> BootstrapResult:
    """Idempotent. Fast no-op when already healthy; performs recovery when not.

    Args:
        force: Unused by the probe short-circuit itself — accepted so callers (and tests)
            can express "run the expensive arm regardless" by calling the mode-specific
            helpers directly if ever needed. The probe already re-verifies from scratch on
            every call, so there is no cached "healthy" state to bust.

    Returns:
        A :class:`BootstrapResult` describing the outcome.
    """
    del force  # See docstring: nothing here is cached across calls to force-bust.

    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        logger.debug("Neural search disabled, skipping bootstrap")
        return BootstrapResult(state="disabled", stage=None, detail=None, model_id=None)

    if neural_search_ready():
        from app.services.search.ml_model_service import get_ml_model_service

        model_id = get_ml_model_service().get_active_model_id()
        return BootstrapResult(state="ok", stage=None, detail=None, model_id=model_id)

    try:
        from app.services.search.ml_model_service import get_ml_model_service

        ml_service = get_ml_model_service()

        if _managed_embedding_mode():
            return _adopt_managed_embedding_model(ml_service)

        return _bootstrap_local_mode(ml_service)

    except Exception as e:
        detail = f"Error initializing neural search: {e}"
        logger.error(detail)
        return BootstrapResult(state="degraded", stage=None, detail=detail, model_id=None)


# --- Redis-backed attempt bookkeeping (issue #625, mechanism 2d) -----------------------


def _text_only_chunk_files_count() -> int:
    """Report-only count of FILES with at least one text-only (non-neural) chunk (#626).

    Report-only, deliberately — David's call: no auto re-embed here. A separate,
    operator-triggered re-embed action is tracked by #626.

    A chunk is text-only when it carries **no `embedding_model` field at all**
    (``EMBEDDING_MODEL_ABSENT`` in ``embedding_provenance.py``) — the state a document is
    written in when the ingest pipeline could not resolve a model at index time. This is
    deliberately NOT the ``"neural"`` sentinel (``EMBEDDING_MODEL_UNKNOWN``): that value means
    "embedded, but the pipeline that did it predates provenance tracking" (#437's legacy
    bucket), a different and much larger population that says nothing about #625's bootstrap
    gap. Counting it here would vastly overstate this specific defect on any pre-#437 index.

    Counts distinct files (a cardinality agg over ``file_uuid``), not documents — a file with
    ten stranded chunks is one entry in the operator-facing number, matching how
    ``search_index_maintenance``'s own "unindexed files" count is framed.
    """
    try:
        from opensearchpy.exceptions import OpenSearchException

        from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
        from app.services.opensearch_service import opensearch_client

        if not opensearch_client:
            return 0

        index = settings.OPENSEARCH_CHUNKS_INDEX
        if not opensearch_client.indices.exists(index=index):
            return 0

        response = opensearch_client.search(
            index=index,
            body={
                "size": 0,
                "query": {
                    "bool": {
                        "filter": [chunk_plane_clause()],
                        "must_not": [{"exists": {"field": "embedding_model"}}],
                    }
                },
                "aggs": {"files": {"cardinality": {"field": "file_uuid"}}},
            },
        )
        return int(response.get("aggregations", {}).get("files", {}).get("value", 0))
    except OpenSearchException as e:
        logger.warning(f"Could not count text-only chunk files: {e}")
        return 0
    except Exception as e:
        logger.warning(f"Could not count text-only chunk files: {e}")
        return 0


def bootstrap_status() -> dict[str, Any]:
    """Read-only snapshot for ``GET /search/models/neural/status``'s ``bootstrap`` block.

    Derives ``state`` from the cheap probe every call — this is a measurement, not a cached
    setting (see the package's "derive, don't record" rule). Attempt history, unlike the
    health state itself, genuinely cannot be derived, so it is read from Redis.
    """
    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        return {
            "state": "disabled",
            "attempts": 0,
            "last_error": None,
            "retry_at": None,
            "text_only_chunk_files": 0,
        }

    state = "ok" if neural_search_ready() else "degraded"

    attempts = 0
    last_error: str | None = None
    retry_at: str | None = None
    try:
        from app.core.redis import get_redis

        r = get_redis()
        raw_attempts = r.get(_ATTEMPTS_KEY)
        attempts = int(raw_attempts) if raw_attempts else 0
        raw_error = r.get(_LAST_ERROR_KEY)
        last_error = raw_error.decode() if isinstance(raw_error, bytes) else raw_error
        raw_next_at = r.get(_NEXT_AT_KEY)
        if raw_next_at:
            from datetime import UTC
            from datetime import datetime

            next_at_ts = float(raw_next_at)
            retry_at = datetime.fromtimestamp(next_at_ts, tz=UTC).isoformat()
    except Exception as e:
        logger.warning(f"Could not read neural bootstrap attempt state: {e}")

    return {
        "state": state,
        "attempts": attempts,
        "last_error": last_error,
        "retry_at": retry_at,
        "text_only_chunk_files": _text_only_chunk_files_count(),
    }


def run_bootstrap_tick() -> dict[str, Any]:
    """One self-heal tick: probe, and only pay the expensive arm when unhealthy and due.

    Called by ``app/tasks/search_maintenance_task.neural_search_bootstrap_task`` every 10
    minutes via beat. Backoff saturates at ``NEURAL_BOOTSTRAP_MAX_BACKOFF_SECONDS`` and never
    terminates — a deployment that cannot reach OpenSearch keeps retrying every 6h forever,
    which is the whole point of a self-heal: it never gives up the way the old one-shot did.

    Redis failure fails OPEN — attempts the bootstrap anyway, matching
    ``app/utils/boot_once.py``'s documented rule that duplicated work is safer than skipped
    work.

    Returns:
        Dict with at least ``state``; ``attempts``/``retry_at`` when relevant.
    """
    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        return {"state": "disabled"}

    if neural_search_ready():
        _clear_attempt_state()
        return {"state": "ok"}

    now = time.time()
    redis_client = None
    try:
        from app.core.redis import get_redis

        redis_client = get_redis()
    except Exception as e:
        logger.warning(f"Neural bootstrap: Redis unavailable ({e}); attempting bootstrap anyway")

    if redis_client is not None:
        try:
            raw_next_at = redis_client.get(_NEXT_AT_KEY)
            if raw_next_at and float(raw_next_at) > now:
                attempts_raw = redis_client.get(_ATTEMPTS_KEY)
                attempts = int(attempts_raw) if attempts_raw else 0
                return {
                    "state": "backoff",
                    "attempts": attempts,
                    "retry_at": float(raw_next_at),
                }
        except Exception as e:
            logger.warning(f"Neural bootstrap: could not read backoff state ({e}); proceeding")

    attempts = _increment_attempts(redis_client)

    result = ensure_neural_search_bootstrap()

    if result.state == "ok":
        _clear_attempt_state(redis_client)
        logger.info("Neural search bootstrap recovered after %d attempt(s)", attempts)
        return {"state": "ok", "attempts": attempts}

    error_text = f"{result.stage}: {result.detail}"
    delay = min(
        NEURAL_BOOTSTRAP_BASE_BACKOFF_SECONDS * (2 ** (attempts - 1)),
        NEURAL_BOOTSTRAP_MAX_BACKOFF_SECONDS,
    )
    next_at = now + delay
    _record_failure(redis_client, error_text, next_at)

    log_level = (
        logging.ERROR if attempts >= NEURAL_BOOTSTRAP_ALERT_AFTER_ATTEMPTS else logging.WARNING
    )
    logger.log(
        log_level,
        "Neural search bootstrap still degraded after %d attempt(s): %s (retrying in %ds)",
        attempts,
        error_text,
        delay,
    )
    return {
        "state": "degraded",
        "attempts": attempts,
        "retry_at": next_at,
        "last_error": error_text,
    }


def _increment_attempts(redis_client: Any) -> int:
    if redis_client is None:
        return 1
    try:
        attempts = int(redis_client.incr(_ATTEMPTS_KEY))
        redis_client.expire(_ATTEMPTS_KEY, _ATTEMPT_KEY_TTL_SECONDS)
        return attempts
    except Exception as e:
        logger.warning(f"Neural bootstrap: could not increment attempt counter ({e})")
        return 1


def _record_failure(redis_client: Any, error_text: str, next_at: float) -> None:
    if redis_client is None:
        return
    try:
        redis_client.set(_LAST_ERROR_KEY, error_text, ex=_ATTEMPT_KEY_TTL_SECONDS)
        redis_client.set(_NEXT_AT_KEY, str(next_at), ex=_ATTEMPT_KEY_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"Neural bootstrap: could not record failure state ({e})")


def _clear_attempt_state(redis_client: Any = None) -> None:
    try:
        if redis_client is None:
            from app.core.redis import get_redis

            redis_client = get_redis()
        redis_client.delete(_ATTEMPTS_KEY, _NEXT_AT_KEY, _LAST_ERROR_KEY)
    except Exception as e:
        logger.warning(f"Neural bootstrap: could not clear attempt state ({e})")
