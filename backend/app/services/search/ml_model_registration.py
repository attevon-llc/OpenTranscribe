"""ML Commons model registration: online (HuggingFace) and offline (local file) paths.

Supports both online registration by name and offline registration from the artifacts
under ``/ml-models`` for air-gapped deployments. Mixed into
:class:`~app.services.search.ml_model_service.OpenSearchMLModelService`.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from app.services.search.ml_model_host import MLModelHost

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


class MLModelRegistrationMixin(MLModelHost):
    """Register models with ML Commons (mixed into ``OpenSearchMLModelService``)."""

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

    def _wait_for_registration(
        self,
        task_id: str,
        *,
        max_wait: float = _REGISTRATION_MAX_WAIT,
        poll_interval: float = _REGISTRATION_POLL_INTERVAL,
    ) -> str | None:
        """Wait for model registration task to complete.

        Args:
            task_id: The registration task ID.
            max_wait: Ceiling in seconds. Defaults to the module constant; only
                overridden by tests so they don't pay the real ~300s ceiling.
            poll_interval: Seconds between polls. Same override rationale as `max_wait`.

        Returns:
            Model ID if registration succeeded, None otherwise.
        """
        start_time = time.time()

        while (time.time() - start_time) < max_wait:
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
                time.sleep(poll_interval)

            except Exception as e:
                logger.warning(f"Error polling registration task: {e}")
                time.sleep(poll_interval)

        logger.error(f"Model registration timed out after {max_wait}s")
        return None
