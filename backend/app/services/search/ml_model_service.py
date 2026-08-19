"""OpenSearch ML Commons model management service for neural search.

This service manages ML models in OpenSearch for native neural search,
eliminating the need for client-side embedding generation.

Supports both online (HuggingFace) and offline (local file) model registration
for air-gapped deployments.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from app.services.opensearch_service import get_opensearch_client

logger = logging.getLogger(__name__)

#: Characters OpenSearch ML Commons accepts in a model description. Anything else
#: fails the WHOLE registration with `action_request_validation_exception`, so the
#: description — pure metadata nobody's code reads back — must never be able to
#: sink the operation. Found live: the registry description "50+ languages" made
#: the Settings UI's Download & deploy 500 on its first real use, because `+` is
#: not in the allowed set.
_DESCRIPTION_ALLOWED = re.compile(r"[^A-Za-z0-9 .,!?():@\-_'/\"]")


def _safe_description(description: str) -> str:
    """Reduce a description to the charset ML Commons accepts.

    Disallowed characters become spaces (then collapsed) rather than being
    deleted, so "50+ languages" degrades to "50 languages" instead of fusing
    words together.
    """
    return re.sub(r"  +", " ", _DESCRIPTION_ALLOWED.sub(" ", description)).strip()


# Model registration task polling
_REGISTRATION_POLL_INTERVAL = 2.0  # seconds
_REGISTRATION_MAX_WAIT = 300  # 5 minutes max wait for model registration
_DEPLOYMENT_POLL_INTERVAL = 2.0  # seconds
_DEPLOYMENT_MAX_WAIT = 120  # 2 minutes max wait for deployment

#: Probe text for the post-deploy inference check (#503). Ordinary prose, because
#: the point is to exercise the normal path rather than an edge case.
_VERIFICATION_TEXT = "the quarterly planning meeting covered budget and hiring"

# Local model storage path (inside OpenSearch container, mounted from host)
_LOCAL_MODEL_PATH = Path("/ml-models")

# Model file naming patterns from OpenSearch artifacts
# Format: {model_short_name}-{version}-{format}.zip
# Example: sentence-transformers_all-MiniLM-L6-v2-1.0.1-torch_script.zip
_MODEL_FILE_PATTERNS = {
    "huggingface/sentence-transformers/all-MiniLM-L6-v2": {
        "short_name": "all-MiniLM-L6-v2",
        "filename": "sentence-transformers_all-MiniLM-L6-v2-1.0.1-torch_script.zip",
        "version": "1.0.1",
    },
    "huggingface/sentence-transformers/all-mpnet-base-v2": {
        "short_name": "all-mpnet-base-v2",
        "filename": "sentence-transformers_all-mpnet-base-v2-1.0.1-torch_script.zip",
        "version": "1.0.1",
    },
    "huggingface/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": {
        "short_name": "paraphrase-multilingual-MiniLM-L12-v2",
        "filename": "sentence-transformers_paraphrase-multilingual-MiniLM-L12-v2-1.0.1-torch_script.zip",
        "version": "1.0.1",
    },
    "huggingface/sentence-transformers/all-MiniLM-L12-v2": {
        "short_name": "all-MiniLM-L12-v2",
        "filename": "sentence-transformers_all-MiniLM-L12-v2-1.0.1-torch_script.zip",
        "version": "1.0.1",
    },
    "huggingface/sentence-transformers/multi-qa-MiniLM-L6-cos-v1": {
        "short_name": "multi-qa-MiniLM-L6-cos-v1",
        "filename": "sentence-transformers_multi-qa-MiniLM-L6-cos-v1-1.0.1-torch_script.zip",
        "version": "1.0.1",
    },
    "huggingface/sentence-transformers/all-distilroberta-v1": {
        "short_name": "all-distilroberta-v1",
        "filename": "sentence-transformers_all-distilroberta-v1-1.0.1-torch_script.zip",
        "version": "1.0.1",
    },
    "huggingface/sentence-transformers/distiluse-base-multilingual-cased-v1": {
        "short_name": "distiluse-base-multilingual-cased-v1",
        "filename": "sentence-transformers_distiluse-base-multilingual-cased-v1-1.0.1-torch_script.zip",
        "version": "1.0.1",
    },
}


class OpenSearchMLModelService:
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

    def get_local_model_path(self, model_name: str) -> Path | None:
        """Check if a model exists on the local filesystem.

        Args:
            model_name: Full model name (e.g., 'huggingface/sentence-transformers/all-MiniLM-L6-v2')

        Returns:
            Path to the model zip file if it exists, None otherwise.
        """
        if model_name not in _MODEL_FILE_PATTERNS:
            logger.debug(f"Model {model_name} not in known patterns")
            return None

        pattern = _MODEL_FILE_PATTERNS[model_name]
        model_path = _LOCAL_MODEL_PATH / pattern["short_name"] / pattern["filename"]

        if model_path.exists():
            logger.info(f"Found local model file: {model_path}")
            return model_path

        # Also check directly in /ml-models (flat structure)
        flat_path = _LOCAL_MODEL_PATH / pattern["filename"]
        if flat_path.exists():
            logger.info(f"Found local model file (flat): {flat_path}")
            return flat_path

        logger.debug(f"Local model not found: {model_path}")
        return None

    def get_available_local_models(self) -> list[dict[str, Any]]:
        """List all models available on the local filesystem.

        Returns:
            List of dicts with model name, path, and metadata.
        """
        available: list[dict[str, Any]] = []

        if not _LOCAL_MODEL_PATH.exists():
            logger.debug(f"Local model path does not exist: {_LOCAL_MODEL_PATH}")
            return available

        for model_name, pattern in _MODEL_FILE_PATTERNS.items():
            local_path = self.get_local_model_path(model_name)
            if local_path:
                available.append(
                    {
                        "name": model_name,
                        "short_name": pattern["short_name"],
                        "path": str(local_path),
                        "version": pattern["version"],
                        "local": True,
                    }
                )

        logger.info(f"Found {len(available)} local models: {[m['short_name'] for m in available]}")
        return available

    def register_model_from_url(
        self,
        model_name: str,
        url: str,
        model_version: str = "1.0.1",
        model_format: str = "TORCH_SCRIPT",
        description: str = "",
        model_config: dict[str, Any] | None = None,
        model_content_hash_value: str | None = None,
    ) -> str | None:
        """Register a model from a URL (file:// or https://).

        This is the OFFLINE path: it registers from the artifact under ``/ml-models``
        instead of letting OpenSearch fetch from the internet.

        ⚠️ **``model_config`` is REQUIRED and its absence is a silent-looking 400.**
        Registering by *name* (the online path, :meth:`register_model`) lets
        OpenSearch look the configuration up from its own registry, so that call
        needs only a name and version. Registering from a URL gives it a zip and
        nothing else, so the caller must supply what the registry would have:

            POST /_plugins/_ml/models/_register {"name", "version", "model_format", "url"}
            -> 400 illegal_argument_exception: "model config is null"

        Measured against ``opensearchproject/opensearch:3.4.0``, the version this
        repo ships. Every offline/airgapped deployment therefore failed to register
        its embedding model and fell back to the remote HuggingFace download — the
        one thing an airgapped install cannot do.

        The values are **not** hardcoded here. OpenSearch publishes a ``config.json``
        beside every artifact carrying the exact ``model_config``, the content hash
        and the true size; ``scripts/download-models.py`` saves it next to the zip and
        :meth:`register_local_model` reads it. A per-model table of model types in
        our source would be a guess that drifts from upstream — and the measured
        types are not guessable (``all-mpnet-base-v2`` is ``mpnet``,
        ``all-distilroberta-v1`` is ``roberta``, the MiniLMs are ``bert``).

        Args:
            model_name: Name to register the model under.
            url: URL to the model zip file (file:// for local, https:// for remote).
            model_version: Model version string.
            model_format: Model format - TORCH_SCRIPT or ONNX.
            description: Optional description.
            model_config: The ``model_config`` block from the artifact's config.json.
                Required by OpenSearch for URL registration.
            model_content_hash_value: SHA-256 of the artifact, from the same file.
                OpenSearch verifies it, so it is integrity checking for free.

        Returns:
            Model ID if registration succeeded, None on error.
        """
        if not self._ensure_client():
            return None

        if not model_config:
            logger.error(
                f"Cannot register {model_name} from {url}: no model_config. OpenSearch "
                "rejects URL registration without it (400 'model config is null'). It "
                "comes from the config.json that scripts/download-models.py saves beside "
                "the artifact — re-run the model download to fetch it."
            )
            return None

        # Ensure ML settings are configured
        self.configure_ml_settings()

        try:
            register_body: dict[str, Any] = {
                "name": model_name,
                "version": model_version,
                "model_format": model_format,
                "url": url,
                "model_config": model_config,
            }

            if model_content_hash_value:
                register_body["model_content_hash_value"] = model_content_hash_value

            if description:
                register_body["description"] = _safe_description(description)

            logger.info(f"Registering model from URL: {model_name} -> {url}")

            assert self._client is not None
            response = self._client.transport.perform_request(
                "POST",
                "/_plugins/_ml/models/_register",
                body=register_body,
            )

            task_id = response.get("task_id")
            if not task_id:
                logger.error(f"No task_id returned for URL model registration: {response}")
                return None

            logger.info(f"Model registration started from URL: {model_name}, task_id={task_id}")

            # Poll for completion
            model_id = self._wait_for_registration(task_id)
            if model_id:
                logger.info(f"Model registered from URL: {model_name} -> {model_id}")
            return model_id

        except Exception as e:
            logger.error(f"Failed to register model from URL {model_name}: {e}")
            return None

    def register_model_from_local(self, model_name: str) -> str | None:
        """Register a model from the local filesystem.

        Checks if the model exists locally and registers it using file:// URL.

        Args:
            model_name: Full model name (e.g., 'huggingface/sentence-transformers/all-MiniLM-L6-v2')

        Returns:
            Model ID if registration succeeded, None if not available locally.
        """
        local_path = self.get_local_model_path(model_name)
        if not local_path:
            logger.debug(f"Model {model_name} not available locally")
            return None

        # Get version from patterns
        version = "1.0.1"
        if model_name in _MODEL_FILE_PATTERNS:
            version = _MODEL_FILE_PATTERNS[model_name]["version"]

        # The artifact's published config.json, saved beside the zip by
        # scripts/download-models.py. OpenSearch REQUIRES its `model_config` for URL
        # registration and 400s without it, so a cache holding only the zip cannot be
        # registered — see register_model_from_url for the measurement.
        model_config, content_hash = self._read_local_model_config(local_path)
        if not model_config:
            logger.error(
                f"Model {model_name} is cached at {local_path} but its config.json is "
                "missing, so it cannot be registered offline. Re-run "
                "scripts/download-models.sh to fetch it."
            )
            return None

        # Build file:// URL
        file_url = f"file://{local_path}"

        return self.register_model_from_url(
            model_name=model_name,
            url=file_url,
            model_version=version,
            model_format="TORCH_SCRIPT",
            description=f"Registered from local file: {local_path}",
            model_config=model_config,
            model_content_hash_value=content_hash,
        )

    @staticmethod
    def _read_local_model_config(local_path: Path) -> tuple[dict[str, Any] | None, str | None]:
        """Read ``model_config`` and the content hash from the artifact's config.json.

        OpenSearch publishes the file at the same prefix as the zip; the downloader
        saves it in the same directory. Returns ``(None, None)`` when it is absent,
        which the caller reports rather than attempting a registration that OpenSearch
        will reject.
        """
        config_path = Path(local_path).parent / "config.json"
        if not config_path.is_file():
            return None, None
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(f"Could not read model config at {config_path}: {exc}")
            return None, None

        model_config = payload.get("model_config")
        if not isinstance(model_config, dict) or not model_config.get("model_type"):
            # `model_type` specifically: OpenSearch rejects its absence with
            # "model type is null", a different 400 from the missing-config one.
            logger.error(f"{config_path} carries no usable model_config: {model_config!r}")
            return None, None
        return model_config, payload.get("model_content_hash_value")

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

    def register_model(
        self,
        model_name: str,
        model_version: str = "1.0.1",
        model_format: str = "TORCH_SCRIPT",
        description: str = "",
    ) -> str | None:
        """Register a pretrained model in OpenSearch.

        Args:
            model_name: HuggingFace model name (e.g., 'sentence-transformers/all-MiniLM-L6-v2')
            model_version: Model version string
            model_format: Model format - TORCH_SCRIPT or ONNX
            description: Optional description

        Returns:
            Model ID if registration started successfully, None on error.
        """
        if not self._ensure_client():
            return None

        # Ensure ML settings are configured
        self.configure_ml_settings()

        try:
            # Build the model registration request
            register_body: dict[str, Any] = {
                "name": model_name,
                "version": model_version,
                "model_format": model_format,
            }

            if description:
                register_body["description"] = _safe_description(description)

            # Register the model
            assert self._client is not None
            response = self._client.transport.perform_request(
                "POST",
                "/_plugins/_ml/models/_register",
                body=register_body,
            )

            task_id = response.get("task_id")
            if not task_id:
                logger.error(f"No task_id returned for model registration: {response}")
                return None

            logger.info(f"Model registration started: {model_name}, task_id={task_id}")

            # Poll for completion
            model_id = self._wait_for_registration(task_id)
            if model_id:
                logger.info(f"Model registered successfully: {model_name} -> {model_id}")
            return model_id

        except Exception as e:
            logger.error(f"Failed to register model {model_name}: {e}")
            return None

    def _wait_for_registration(self, task_id: str) -> str | None:
        """Wait for model registration task to complete.

        Args:
            task_id: The registration task ID.

        Returns:
            Model ID if registration succeeded, None otherwise.
        """
        start_time = time.time()

        while (time.time() - start_time) < _REGISTRATION_MAX_WAIT:
            try:
                assert self._client is not None
                task_response = self._client.transport.perform_request(
                    "GET",
                    f"/_plugins/_ml/tasks/{task_id}",
                )

                state = task_response.get("state", "").upper()

                if state == "COMPLETED":
                    model_id: str | None = task_response.get("model_id")
                    return model_id

                if state in ("FAILED", "FAILED_REGISTER"):
                    error = task_response.get("error", "Unknown error")
                    logger.error(f"Model registration failed: {error}")
                    return None

                # Still in progress
                logger.debug(f"Registration task {task_id} state: {state}")
                time.sleep(_REGISTRATION_POLL_INTERVAL)

            except Exception as e:
                logger.warning(f"Error polling registration task: {e}")
                time.sleep(_REGISTRATION_POLL_INTERVAL)

        logger.error(f"Model registration timed out after {_REGISTRATION_MAX_WAIT}s")
        return None

    def deploy_model(self, model_id: str) -> bool:
        """Deploy a registered model to make it ready for inference.

        Args:
            model_id: The OpenSearch model ID.

        Returns:
            True if deployment succeeded.
        """
        if not self._ensure_client():
            return False

        try:
            assert self._client is not None
            response = self._client.transport.perform_request(
                "POST",
                f"/_plugins/_ml/models/{model_id}/_deploy",
            )

            task_id = response.get("task_id")
            if not task_id:
                # Some models deploy synchronously
                status = response.get("status", "").upper()
                if status == "DEPLOYED":
                    logger.info(f"Model {model_id} deployed (synchronous)")
                    return True
                logger.warning(f"Deploy response without task_id: {response}")
                return False

            # Wait for deployment
            return self._wait_for_deployment(model_id, task_id)

        except Exception as e:
            logger.error(f"Failed to deploy model {model_id}: {e}")
            return False

    def _wait_for_deployment(self, model_id: str, task_id: str) -> bool:
        """Wait for model deployment to complete.

        Args:
            model_id: The model ID.
            task_id: The deployment task ID.

        Returns:
            True if deployment succeeded.
        """
        start_time = time.time()

        while (time.time() - start_time) < _DEPLOYMENT_MAX_WAIT:
            try:
                # Check task status
                assert self._client is not None
                task_response = self._client.transport.perform_request(
                    "GET",
                    f"/_plugins/_ml/tasks/{task_id}",
                )

                state = task_response.get("state", "").upper()

                if state == "COMPLETED":
                    logger.info(f"Model {model_id} deployed successfully")
                    return True

                if state == "FAILED":
                    error = task_response.get("error", "Unknown error")
                    logger.error(f"Model deployment failed: {error}")
                    return False

                logger.debug(f"Deployment task {task_id} state: {state}")
                time.sleep(_DEPLOYMENT_POLL_INTERVAL)

            except Exception as e:
                logger.warning(f"Error polling deployment task: {e}")
                time.sleep(_DEPLOYMENT_POLL_INTERVAL)

        logger.error(f"Model deployment timed out after {_DEPLOYMENT_MAX_WAIT}s")
        return False

    def _verified_or_none(self, model_name: str, model_id: str) -> str | None:
        """Return the id only if the model can ACTUALLY embed (#503).

        Every return path of :meth:`ensure_model_deployed` goes through here, because a
        model that reports deployed and cannot embed is worse than one that failed to
        deploy: the caller stores it as the active model, the ingest pipeline points at
        it, and every document indexed afterwards silently carries no vector.

        Refusing the id is the loud outcome — the caller already logs
        "Could not deploy default model" and neural search stays off, which is a state
        an operator can see and act on.
        """
        from app.services.search.settings_service import get_search_embedding_dimension

        try:
            expected = get_search_embedding_dimension()
        except Exception:  # noqa: BLE001 - a settings read must not break deployment
            expected = None

        ok, detail = self.verify_model_can_embed(model_id, expected_dimension=expected)
        if ok:
            logger.info(f"Verified {model_name} can embed: {detail}")
            return model_id

        logger.error(
            f"Model {model_name} ({model_id}) reports deployed but CANNOT EMBED: {detail}. "
            "Refusing to activate it. A model that deploys and cannot embed leaves every "
            "later document without a vector and no error anywhere. If the cluster is "
            "small, note the measured floors: 1 GB heap runs all-MiniLM-L6-v2, 2 GB runs "
            "every English model (see docker-compose.yml)."
        )
        return None

    def verify_model_can_embed(
        self, model_id: str, expected_dimension: int | None = None
    ) -> tuple[bool, str]:
        """Run a REAL prediction and check the vector that comes back (#503).

        ⚠️ **"Deployed" is not "working", and the difference is measured.**
        ``all-mpnet-base-v2`` on a 1 GB heap reports ``DEPLOY: COMPLETED`` and then
        fails to produce an embedding — so :meth:`_wait_for_deployment`, which returns
        True on that state, reported a cluster that could not embed as healthy. Every
        document indexed afterwards silently gets no vector, and neural search quietly
        degrades to BM25 with nothing in the logs saying so.

        This is also the cheapest possible check for "does this model exist and work
        here at all", which is the #504 failure (a model offered in nine files that
        OpenSearch does not provide) caught one layer earlier.

        Args:
            model_id: The deployed ML Commons model id.
            expected_dimension: Vector width the index is configured for. When given, a
                mismatch is a failure — writing 768-d vectors into a 384-d ``knn_vector``
                is rejected per document, which surfaces as an empty index rather than
                an error anyone reads.

        Returns:
            ``(ok, detail)``. ``detail`` names the cause on failure, for the log line.
        """
        if not self._ensure_client():
            return False, "OpenSearch client unavailable"

        try:
            assert self._client is not None
            response = self._client.transport.perform_request(
                "POST",
                f"/_plugins/_ml/_predict/text_embedding/{model_id}",
                body={
                    "text_docs": [_VERIFICATION_TEXT],
                    "return_number": True,
                    "target_response": ["sentence_embedding"],
                },
            )
        except Exception as exc:  # noqa: BLE001 - any failure means "cannot embed"
            return False, f"prediction request failed: {exc}"

        try:
            vector = response["inference_results"][0]["output"][0]["data"]
        except (KeyError, IndexError, TypeError):
            return False, f"prediction returned no embedding: {str(response)[:200]}"

        if not vector:
            return False, "prediction returned an empty vector"

        if expected_dimension is not None and len(vector) != expected_dimension:
            return False, (
                f"model returns {len(vector)}-dimension vectors but the index is "
                f"configured for {expected_dimension}. Writing these would be rejected "
                "per document and the index would simply stay empty."
            )

        return True, f"{len(vector)}-dimension embedding"

    def undeploy_model(self, model_id: str) -> bool:
        """Undeploy a model from memory.

        Args:
            model_id: The OpenSearch model ID.

        Returns:
            True if undeploy succeeded.
        """
        if not self._ensure_client():
            return False

        try:
            assert self._client is not None
            self._client.transport.perform_request(
                "POST",
                f"/_plugins/_ml/models/{model_id}/_undeploy",
            )
            logger.info(f"Model {model_id} undeployed")
            return True

        except Exception as e:
            logger.error(f"Failed to undeploy model {model_id}: {e}")
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

    def ensure_model_deployed(self, model_name: str) -> str | None:
        """Ensure a model is registered and deployed.

        For offline/air-gapped deployments, tries to register from local files first.
        Falls back to remote registration (HuggingFace) if local file not available.

        Args:
            model_name: HuggingFace model name.

        Returns:
            Model ID if successful, None on error.
        """
        # Check if already registered
        model_id = self.find_model_by_name(model_name)

        if model_id:
            status = self.get_model_status(model_id)
            if status.get("deployed"):
                logger.info(f"Model {model_name} already deployed: {model_id}")
                return self._verified_or_none(model_name, model_id)

            # Deploy if registered but not deployed
            if self.deploy_model(model_id):
                return self._verified_or_none(model_name, model_id)
            return None

        # Not registered - try local first (for offline deployments)
        local_path = self.get_local_model_path(model_name)
        if local_path:
            logger.info(f"Registering model from local file: {model_name}")
            model_id = self.register_model_from_local(model_name)
            if model_id and self.deploy_model(model_id):
                return self._verified_or_none(model_name, model_id)
            # Local registration failed, continue to try remote
            logger.warning(f"Local model registration failed for {model_name}, trying remote")

        # Try remote registration (requires internet)
        logger.info(f"Registering model from remote: {model_name}")
        model_id = self.register_model(model_name)
        if not model_id:
            return None

        if self.deploy_model(model_id):
            return self._verified_or_none(model_name, model_id)

        return None

    def create_ingest_pipeline(
        self,
        pipeline_id: str,
        model_id: str,
        source_field: str = "content",
        target_field: str = "embedding",
        description: str = "",
    ) -> bool:
        """Create a neural ingest pipeline with text_embedding processor.

        Args:
            pipeline_id: ID for the ingest pipeline.
            model_id: The deployed model ID to use.
            source_field: Field to read text from.
            target_field: Field to write embedding to.
            description: Optional pipeline description.

        Returns:
            True if pipeline was created successfully.
        """
        if not self._ensure_client():
            return False

        try:
            pipeline_body: dict[str, Any] = {
                "description": description or f"Neural embedding pipeline using model {model_id}",
                "processors": [
                    {
                        "text_embedding": {
                            "model_id": model_id,
                            "field_map": {source_field: target_field},
                        }
                    }
                ],
            }

            assert self._client is not None
            self._client.ingest.put_pipeline(id=pipeline_id, body=pipeline_body)
            logger.info(f"Created ingest pipeline: {pipeline_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to create ingest pipeline {pipeline_id}: {e}")
            return False

    def delete_ingest_pipeline(self, pipeline_id: str) -> bool:
        """Delete an ingest pipeline.

        Args:
            pipeline_id: ID of the pipeline to delete.

        Returns:
            True if deletion succeeded.
        """
        if not self._ensure_client():
            return False

        try:
            assert self._client is not None
            self._client.ingest.delete_pipeline(id=pipeline_id)
            logger.info(f"Deleted ingest pipeline: {pipeline_id}")
            return True

        except Exception as e:
            logger.debug(f"Could not delete pipeline {pipeline_id}: {e}")
            return False

    def get_ingest_pipeline(self, pipeline_id: str) -> dict[str, Any] | None:
        """Get an ingest pipeline configuration.

        Args:
            pipeline_id: ID of the pipeline.

        Returns:
            Pipeline configuration dict, or None if not found.
        """
        if not self._ensure_client():
            return None

        try:
            assert self._client is not None
            response = self._client.ingest.get_pipeline(id=pipeline_id)
            result: dict[str, Any] | None = response.get(pipeline_id)
            return result

        except Exception as e:
            logger.debug(f"Pipeline {pipeline_id} not found: {e}")
            return None

    def update_ingest_pipeline_model(self, pipeline_id: str, new_model_id: str) -> bool:
        """Update an existing ingest pipeline with a new model.

        Args:
            pipeline_id: ID of the pipeline to update.
            new_model_id: The new model ID to use.

        Returns:
            True if update succeeded.
        """
        current = self.get_ingest_pipeline(pipeline_id)
        if not current:
            logger.warning(f"Pipeline {pipeline_id} not found, cannot update")
            return False

        # Update the model_id in the text_embedding processor
        processors = current.get("processors", [])
        for processor in processors:
            if "text_embedding" in processor:
                processor["text_embedding"]["model_id"] = new_model_id

        return self.create_ingest_pipeline(
            pipeline_id=pipeline_id,
            model_id=new_model_id,
            description=current.get("description", ""),
        )

    def get_active_model_id(self) -> str | None:
        """Get the currently active model ID from settings.

        Returns the model_id stored in the database settings, or adopts the one
        deployed model as a fallback when the stored one is gone.

        **The fallback refuses to choose between candidates** (issue #437). It
        used to return ``list_models(deployed_only=True)[0]`` — the first hit of a
        ``match_all`` search with no sort, i.e. an arbitrary model. The caller
        that matters is
        :func:`~app.services.search.indexing_service.ensure_neural_ingest_pipeline`,
        which writes whatever it is handed into the ingest pipeline; the drift
        check then sees the model change and recreates the pipeline. So an
        arbitrary pick silently repoints embedding at a different model **with no
        user action at all**, and every document indexed afterwards lands in the
        same kNN space as documents from the previous one. Nothing about the
        result looks wrong.

        Exactly one deployed model is not a choice, and adopting it is what
        recovers neural search after the stored id goes stale (an OpenSearch
        volume lost and the model re-registered under a new id — the case this
        fallback was written for). It is still logged, because it is still a
        change of model, and the resulting documents are now labelled with it.
        More than one deployed model **is** a choice, and the right move is to
        make the operator make it: returning ``None`` degrades search to BM25,
        which is loud, obvious and reversible, where the wrong guess is silent
        and costs a full reindex.

        Returns:
            Model ID string, or None when there is no unambiguous active model.
        """
        # First check database/settings for configured model
        from app.services.search.settings_service import _get_setting

        stored_model_id = _get_setting("search.opensearch_model_id")
        if stored_model_id:
            # Verify it's actually deployed
            status = self.get_model_status(stored_model_id)
            if status.get("deployed"):
                return stored_model_id
            logger.warning(f"Stored model {stored_model_id} is not deployed")

        deployed = self.list_models(deployed_only=True)
        if len(deployed) == 1:
            adopted = deployed[0].get("model_id")
            logger.warning(
                f"No active embedding model is configured; adopting the only deployed "
                f"model {adopted} ({deployed[0].get('name')}). If that is not the model "
                f"the existing index was built with, its vectors are not comparable and "
                f"a full reindex is required."
            )
            return adopted
        if len(deployed) > 1:
            names = ", ".join(f"{m.get('name')} ({m.get('model_id')})" for m in deployed)
            logger.error(
                f"No active embedding model is configured and {len(deployed)} are "
                f"deployed, so there is no unambiguous choice: {names}. Neural search "
                f"stays OFF until one is set via PUT /search/models/neural/active — "
                f"picking one here would silently embed new documents with a different "
                f"model than the index already holds."
            )

        return None

    def set_active_model_id(self, model_id: str) -> None:
        """Persist the active model ID to database settings.

        Args:
            model_id: The model ID to set as active.
        """
        from app.services.search.settings_service import _set_setting

        _set_setting(
            "search.opensearch_model_id",
            model_id,
            "OpenSearch ML model ID for neural search",
        )
        logger.info(f"Set active OpenSearch model: {model_id}")


# Module-level singleton
_ml_model_service: OpenSearchMLModelService | None = None


def get_ml_model_service() -> OpenSearchMLModelService:
    """Get the ML model service singleton."""
    global _ml_model_service
    if _ml_model_service is None:
        _ml_model_service = OpenSearchMLModelService()
    return _ml_model_service
