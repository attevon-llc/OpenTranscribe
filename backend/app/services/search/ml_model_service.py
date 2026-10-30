"""OpenSearch ML Commons model management service for neural search.

This service manages ML models in OpenSearch for native neural search,
eliminating the need for client-side embedding generation.

Supports both online (HuggingFace) and offline (local file) model registration
for air-gapped deployments. The service class is composed from three mixins:
registration (``ml_model_registration``), deployment (``ml_model_deployment``) and
ingest pipelines (``ml_ingest_pipelines``). The client is owned HERE (``__init__`` /
``_ensure_client``) and every mixin reaches it through ``self._ensure_client()``, so
patching this module's ``get_opensearch_client`` takes effect for all of them.
"""

import logging
from typing import Any

from app.services.opensearch_service import get_opensearch_client
from app.services.search.ml_ingest_pipelines import MLIngestPipelineMixin
from app.services.search.ml_model_deployment import MLModelDeploymentMixin
from app.services.search.ml_model_registration import MLModelRegistrationMixin

logger = logging.getLogger(__name__)


class OpenSearchMLModelService(
    MLModelDeploymentMixin, MLModelRegistrationMixin, MLIngestPipelineMixin
):
    """Manages ML models in OpenSearch for neural search.

    Provides methods to:
    - Configure ML Commons cluster settings
    - Register pretrained models
    - Deploy/undeploy models
    - Manage neural ingest pipelines
    - Query model status
    """

    def __init__(self):
        self._client = get_opensearch_client()
        self._ml_settings_configured = False

    def _ensure_client(self) -> bool:
        """Ensure OpenSearch client is available."""
        if self._client is None:
            self._client = get_opensearch_client()
        return self._client is not None

    def configure_ml_settings(self) -> bool:
        """Configure cluster settings for ML workloads.

        Sets:
        - plugins.ml_commons.only_run_on_ml_node: false
          (Allow ML tasks on any node, not just dedicated ML nodes)
        - plugins.ml_commons.native_memory_threshold: 99
          (Allow models to use up to 99% of native memory)
        - plugins.ml_commons.jvm_heap_memory_threshold: 95
          (Raised from the 85 default — see below. This is the sibling of the
          native-memory setting above, and leaving it at its default while raising
          that one is what produced the failure this comment exists to explain.)

        ⚠️ **Why the JVM heap threshold is raised (#495 follow-on).** ML Commons
        refuses inference when *instantaneous* JVM heap-used exceeds this percentage,
        and the neural ingest pipeline runs inference for every document — so one
        moment over the line fails an entire bulk load with
        ``circuit_breaking_exception: Memory Circuit Breaker is open``. Measured on
        the dev cluster while it happened: **heap 89%** against the 85 default.

        That was a FALSE trip, not memory exhaustion. Heap-used includes
        uncollected young-generation garbage, and the same node measured
        ``old gen 382 MB`` of a 4 GB heap moments later — a live working set under
        10%, with a 36 MB chunk index. Bulk indexing fills young gen with garbage
        faster than G1 collects it, so a healthy cluster reads as an exhausted one
        for as long as it takes a collection to run.

        95 rather than 99: it matches OpenSearch's own parent-breaker convention and
        keeps a real backstop for genuine exhaustion, which is what this setting is
        for. Disabling it outright would trade a false positive for no protection.

        Returns:
            True if settings were configured successfully.
        """
        if not self._ensure_client():
            logger.warning("OpenSearch client not available, skipping ML settings")
            return False

        if self._ml_settings_configured:
            return True

        try:
            settings_body = {
                "persistent": {
                    "plugins.ml_commons.only_run_on_ml_node": False,
                    "plugins.ml_commons.native_memory_threshold": 99,
                    "plugins.ml_commons.jvm_heap_memory_threshold": 95,
                    "plugins.ml_commons.model_access_control_enabled": False,
                    "plugins.ml_commons.allow_registering_model_via_url": True,
                }
            }

            assert self._client is not None
            self._client.cluster.put_settings(body=settings_body)
            self._ml_settings_configured = True
            logger.info("ML Commons cluster settings configured successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to configure ML Commons settings: {e}")
            return False

    def get_model_status(self, model_id: str) -> dict[str, Any]:
        """Get the status of a registered model.

        Args:
            model_id: The OpenSearch model ID.

        Returns:
            Dict with model state and info, or empty dict on error.
        """
        if not self._ensure_client():
            return {}

        try:
            assert self._client is not None
            response = self._client.transport.perform_request(
                "GET",
                f"/_plugins/_ml/models/{model_id}",
            )

            return {
                "model_id": model_id,
                "name": response.get("name", ""),
                "state": response.get("model_state", "UNKNOWN"),
                "version": response.get("version", ""),
                "model_format": response.get("model_format", ""),
                "deployed": response.get("model_state", "").upper() == "DEPLOYED",
            }

        except Exception as e:
            logger.debug(f"Could not get model status for {model_id}: {e}")
            return {}

    def list_models(self, deployed_only: bool = False) -> list[dict[str, Any]]:
        """List all registered models.

        Args:
            deployed_only: If True, only return deployed models.

        Returns:
            List of model info dicts.
        """
        if not self._ensure_client():
            return []

        try:
            # ⚠️ ML Commons stores model CHUNK documents in this same index, and they
            # carry the same `name` as their parent. A `match_all` therefore returns
            # mostly chunks: on a cluster with two models registered, 172 documents
            # were chunks and the real model docs did not make the `size` window at
            # all. Chunk ids are `<model_id>_<n>`, so `find_model_by_name` handed a
            # CHUNK id to deploy — which 500s, which left the model-switch guard
            # answering 409 forever and made a non-default embedding model impossible
            # to adopt through the app. Chunks are exactly the documents carrying
            # `chunk_number`; model documents never do.
            search_body: dict[str, Any] = {
                "query": {"bool": {"must_not": [{"exists": {"field": "chunk_number"}}]}},
                "size": 100,
            }

            assert self._client is not None
            response = self._client.transport.perform_request(
                "POST",
                "/_plugins/_ml/models/_search",
                body=search_body,
            )

            models = []
            hits = response.get("hits", {}).get("hits", [])

            for hit in hits:
                source = hit.get("_source", {})
                model_state = source.get("model_state", "UNKNOWN").upper()

                if deployed_only and model_state != "DEPLOYED":
                    continue

                models.append(
                    {
                        "model_id": hit.get("_id", ""),
                        "name": source.get("name", ""),
                        "state": model_state,
                        "version": source.get("version", ""),
                        "model_format": source.get("model_format", ""),
                        "deployed": model_state == "DEPLOYED",
                        "algorithm": source.get("algorithm", ""),
                    }
                )

            return models

        except Exception as e:
            logger.error(f"Failed to list models: {e}")
            return []

    def find_model_by_name(self, model_name: str) -> str | None:
        """Find a model ID by its name.

        Args:
            model_name: The model name to search for.

        Returns:
            Model ID if found, None otherwise.
        """
        models = self.list_models()
        for model in models:
            if model.get("name") == model_name:
                return model.get("model_id")
        return None


# Module-level singleton
_ml_model_service: OpenSearchMLModelService | None = None


def get_ml_model_service() -> OpenSearchMLModelService:
    """Get the ML model service singleton."""
    global _ml_model_service
    if _ml_model_service is None:
        _ml_model_service = OpenSearchMLModelService()
    return _ml_model_service
