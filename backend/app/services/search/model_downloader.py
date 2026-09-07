"""OpenSearch neural model downloader for startup initialization.

This module provides functionality to download OpenSearch neural search models
during backend startup, ensuring models are available in the persistent volume
for offline deployments.
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Model registry - must match _MODEL_FILE_PATTERNS in ml_model_service.py
_OPENSEARCH_MODEL_REGISTRY = {
    "huggingface/sentence-transformers/all-MiniLM-L6-v2": {
        "short_name": "all-MiniLM-L6-v2",
        "filename": "sentence-transformers_all-MiniLM-L6-v2-1.0.1-torch_script.zip",
        "version": "1.0.1",
        "dimension": 384,
        "size_mb": 80,
        "url_base": "https://artifacts.opensearch.org/models/ml-models",
    },
    "huggingface/sentence-transformers/all-mpnet-base-v2": {
        "short_name": "all-mpnet-base-v2",
        "filename": "sentence-transformers_all-mpnet-base-v2-1.0.1-torch_script.zip",
        "version": "1.0.1",
        "dimension": 768,
        "size_mb": 420,
        "url_base": "https://artifacts.opensearch.org/models/ml-models",
    },
    "huggingface/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": {
        "short_name": "paraphrase-multilingual-MiniLM-L12-v2",
        "filename": "sentence-transformers_paraphrase-multilingual-MiniLM-L12-v2-1.0.1-torch_script.zip",
        "version": "1.0.1",
        "dimension": 384,
        "size_mb": 420,
        "url_base": "https://artifacts.opensearch.org/models/ml-models",
    },
    "huggingface/sentence-transformers/all-MiniLM-L12-v2": {
        "short_name": "all-MiniLM-L12-v2",
        "filename": "sentence-transformers_all-MiniLM-L12-v2-1.0.1-torch_script.zip",
        "version": "1.0.1",
        "dimension": 384,
        "size_mb": 120,
        "url_base": "https://artifacts.opensearch.org/models/ml-models",
    },
    "huggingface/sentence-transformers/multi-qa-MiniLM-L6-cos-v1": {
        "short_name": "multi-qa-MiniLM-L6-cos-v1",
        "filename": "sentence-transformers_multi-qa-MiniLM-L6-cos-v1-1.0.1-torch_script.zip",
        "version": "1.0.1",
        "dimension": 384,
        "size_mb": 80,
        "url_base": "https://artifacts.opensearch.org/models/ml-models",
    },
    "huggingface/sentence-transformers/all-distilroberta-v1": {
        "short_name": "all-distilroberta-v1",
        "filename": "sentence-transformers_all-distilroberta-v1-1.0.1-torch_script.zip",
        "version": "1.0.1",
        "dimension": 768,
        "size_mb": 290,
        "url_base": "https://artifacts.opensearch.org/models/ml-models",
    },
    "huggingface/sentence-transformers/distiluse-base-multilingual-cased-v1": {
        "short_name": "distiluse-base-multilingual-cased-v1",
        "filename": "sentence-transformers_distiluse-base-multilingual-cased-v1-1.0.1-torch_script.zip",
        "version": "1.0.1",
        "dimension": 512,
        "size_mb": 480,
        "url_base": "https://artifacts.opensearch.org/models/ml-models",
    },
}

# Default model cache directory (backend container path).
# This is the SAME path OpenSearch mounts read-only at /ml-models (docker-compose.yml),
# backed by ${MODEL_CACHE_DIR}/opensearch-ml on the host -- not a private backend-only
# cache. ml_model_service.get_local_model_path() reads from this exact path, so a model
# downloaded anywhere else is invisible to it (issue #638).
_DEFAULT_CACHE_DIR = Path("/ml-models")


def ensure_model_downloaded(model_name: str, cache_dir: Path | None = None) -> Path | None:
    """Ensure a model is downloaded to the local cache.

    Args:
        model_name: Full model name (e.g., 'huggingface/sentence-transformers/all-MiniLM-L6-v2')
        cache_dir: Optional cache directory override (defaults to ~/.cache/opensearch-ml)

    Returns:
        Path to the downloaded model zip file if successful, None otherwise.
    """
    if cache_dir is None:
        cache_dir = _DEFAULT_CACHE_DIR

    if model_name not in _OPENSEARCH_MODEL_REGISTRY:
        logger.warning(f"Model {model_name} not in registry")
        return None

    model_info = _OPENSEARCH_MODEL_REGISTRY[model_name]
    short_name = str(model_info["short_name"])
    filename = str(model_info["filename"])
    version = model_info["version"]

    # Create model directory
    model_dir = cache_dir / short_name
    model_dir.mkdir(parents=True, exist_ok=True)

    model_path = model_dir / filename
    config_path = model_dir / "config.json"
    config_url = f"{model_info['url_base']}/{model_name}/{version}/torch_script/config.json"

    # Check if already downloaded
    if model_path.exists() and model_path.stat().st_size > 0:
        logger.info(
            f"Model already cached: {model_path} ({model_path.stat().st_size / (1024 * 1024):.1f} MB)"
        )
        # An older cache may hold the zip but not the config.json this fetches now
        # (issue #638) -- the zip alone cannot be registered offline, so fetch the
        # missing half rather than reporting a complete cache.
        _fetch_model_config(config_url, config_path)
        return model_path

    # Build download URL
    # Format: https://artifacts.opensearch.org/models/ml-models/{name}/{version}/torch_script/{filename}
    url = f"{model_info['url_base']}/{model_name}/{version}/torch_script/{filename}"

    logger.info(f"Downloading OpenSearch model: {model_name}")
    logger.info(f"  URL: {url}")
    logger.info(f"  Destination: {model_path}")
    logger.info(f"  Expected size: ~{model_info['size_mb']} MB")

    try:
        # Download the model with progress indication
        def progress_hook(block_num: int, block_size: int, total_size: int):
            """Simple progress indicator."""
            if total_size > 0 and block_num % 50 == 0:  # Log every ~5MB for 100KB blocks
                downloaded = block_num * block_size
                percent = (downloaded / total_size) * 100
                logger.info(f"  Downloading: {percent:.1f}% ({downloaded / (1024 * 1024):.1f} MB)")

        urllib.request.urlretrieve(url, model_path, reporthook=progress_hook)  # noqa: S310  # nosec B310

        # Verify download
        if model_path.exists() and model_path.stat().st_size > 0:
            size_mb = model_path.stat().st_size / (1024 * 1024)
            logger.info(f"Model downloaded successfully: {size_mb:.1f} MB")

            # OpenSearch requires model_config for a file:// registration -- without it,
            # OpenSearch 3.4 answers 400 "model config is null" (issue #638). Fetching
            # config.json (published beside every artifact) is what makes offline
            # registration work without hardcoding a per-model type table that would
            # drift from upstream.
            _fetch_model_config(config_url, config_path)

            # Update manifest
            _update_manifest(cache_dir, model_name, model_info)

            return model_path
        else:
            logger.error(f"Download failed - file is empty or missing: {model_path}")
            return None

    except urllib.error.HTTPError as e:
        logger.error(f"HTTP Error {e.code}: {e.reason}")
        logger.error(f"Model may not be available from OpenSearch artifacts: {url}")
        return None
    except urllib.error.URLError as e:
        logger.warning(f"Network error downloading model (this is OK if offline): {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to download model {model_name}: {e}")
        return None


def _fetch_model_config(config_url: str, config_path: Path) -> bool:
    """Fetch the config.json OpenSearch publishes beside a model artifact.

    Registering a model from a ``file://`` URL REQUIRES its ``model_config``; without it
    OpenSearch answers ``400 illegal_argument_exception: model config is null`` (issue #638).
    The file also carries ``model_content_hash_value``, so fetching it buys an integrity
    check for free. Mirrors ``scripts/download-models.py``'s ``_fetch_model_config`` -- keep
    both in sync; the config format is OpenSearch's, not ours.

    Returns True when a non-empty config.json is present afterwards.
    """
    if config_path.exists() and config_path.stat().st_size > 0:
        return True
    try:
        urllib.request.urlretrieve(config_url, config_path)  # noqa: S310  # nosec B310
        return config_path.exists() and config_path.stat().st_size > 0
    except Exception as e:
        logger.warning(
            f"config.json unavailable ({e}); cannot register {config_path.parent.name} offline"
        )
        return False


def _update_manifest(cache_dir: Path, model_name: str, model_info: dict[str, Any]) -> None:
    """Update the model manifest file.

    Args:
        cache_dir: Cache directory path
        model_name: Full model name
        model_info: Model metadata
    """
    manifest_path = cache_dir / "model_manifest.json"

    # Load existing manifest or create new
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read existing manifest: {e}")
            manifest = {}

    # Update manifest
    if "models" not in manifest:
        manifest["models"] = []

    # Remove existing entry for this model
    manifest["models"] = [m for m in manifest["models"] if m.get("name") != model_name]

    # Add updated entry
    manifest["models"].append(
        {
            "name": model_name,
            "short_name": model_info["short_name"],
            "version": model_info["version"],
            "dimension": model_info["dimension"],
            "downloaded_at": datetime.now(UTC).isoformat(),
        }
    )

    manifest["updated_at"] = datetime.now(UTC).isoformat()

    # Write manifest
    try:
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        logger.debug(f"Updated manifest: {manifest_path}")
    except Exception as e:
        logger.warning(f"Could not update manifest: {e}")


def check_internet_connectivity(timeout: float = 5.0) -> bool:
    """Check if internet is available by testing connection to OpenSearch artifacts.

    Args:
        timeout: Connection timeout in seconds

    Returns:
        True if internet is available, False otherwise.
    """
    test_url = "https://artifacts.opensearch.org"
    try:
        req = urllib.request.Request(test_url, method="HEAD")  # noqa: S310  # nosec B310
        urllib.request.urlopen(req, timeout=timeout)  # noqa: S310  # nosec B310
        return True
    except urllib.error.HTTPError:
        # An HTTP error response (even 4xx/5xx) still means DNS resolved, the TCP
        # handshake completed, and TLS negotiated -- the opposite of "no internet".
        # Measured live: a bare HEAD on this bucket's root gets a 403 from
        # CloudFront regardless of real connectivity (S3 buckets commonly reject a
        # rootless HEAD), which made every fresh-install/lite-mode rehearsal on a
        # real network report "No internet connection" and skip straight to
        # OpenSearch's own remote-registration path -- exactly the branch this
        # check exists to avoid needing.
        return True
    except Exception:
        return False
