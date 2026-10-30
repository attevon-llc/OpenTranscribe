"""OpenSearch search-pipeline and neural ingest-pipeline lifecycle for the chunks index.

Owns the hybrid *search* pipeline (``ensure_search_pipeline_exists``), the neural *ingest*
pipeline (``ensure_neural_ingest_pipeline`` and its drift check) and the process-wide
"is the neural pipeline verified" state that gates whether documents are indexed with
server-side embeddings.
"""

import logging
from typing import Any

from app.core.config import settings
from app.services.opensearch_service import opensearch_client

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
