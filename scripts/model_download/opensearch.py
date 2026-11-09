"""The OpenSearch neural-model download group (artifacts.opensearch.org, no HF token)."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from model_download.ui import (
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
)

logger = logging.getLogger(__name__)


def _fetch_model_config(config_url, config_path):
    """Fetch the config.json OpenSearch publishes beside a model artifact.

    Registering a model from a ``file://`` URL REQUIRES its ``model_config``; without
    it OpenSearch 3.4 answers ``400 illegal_argument_exception: model config is null``,
    which is how offline registration was broken. The file also carries
    ``model_content_hash_value``, so fetching it buys an integrity check for free.

    Takes its paths as ARGUMENTS rather than closing over the download loop's
    variables. A closure would late-bind them (ruff B023) and could write one model's
    config into another model's directory.

    Returns True when a non-empty config.json is present afterwards.
    """
    import urllib.request

    if config_path.exists() and config_path.stat().st_size > 0:
        return True
    try:
        urllib.request.urlretrieve(config_url, config_path)
        return config_path.exists() and config_path.stat().st_size > 0
    except OSError as exc:  # URLError/HTTPError/write failure: "no config", reported here
        logger.warning('config.json fetch failed: %s', config_url, exc_info=True)
        print_error(f'  config.json unavailable ({exc}); cannot register this model offline')
        return False


def download_opensearch_neural_models():
    """Download OpenSearch neural search models for offline use.

    Downloads pre-built models from OpenSearch's artifact repository.
    These are the same models OpenSearch ML Commons downloads when registering
    pretrained models, but we pre-download them for offline/air-gapped deployments.

    Environment Variables:
        OPENSEARCH_MODELS: Comma-separated list of model short names to download.
                          Example: "all-MiniLM-L6-v2,all-mpnet-base-v2"
        DOWNLOAD_ALL_OPENSEARCH_MODELS: Set to "true" to download all 6 models.

    Available models (use short names):
        - all-MiniLM-L6-v2 (default, fast English, 80MB)
        - all-mpnet-base-v2 (balanced English, 420MB)
        - all-distilroberta-v1 (best quality English, 290MB)
        - paraphrase-multilingual-MiniLM-L12-v2 (fast multilingual, 420MB)
        - multi-qa-MiniLM-L6-cos-v1 (retrieval-tuned English, 80MB)
        - distiluse-base-multilingual-cased-v1 (best multilingual, 480MB)
    """
    print_header('Downloading OpenSearch Neural Search Models')

    # Complete registry of available models - matches OPENSEARCH_EMBEDDING_MODELS in core/embedding_models.py
    all_available_models: dict[str, dict[str, str | int]] = {
        # Fast tier - 384 dimensions
        'all-MiniLM-L6-v2': {
            'name': 'huggingface/sentence-transformers/all-MiniLM-L6-v2',
            'version': '1.0.1',
            'format': 'torch_script',
            'dimension': 384,
            'size_mb': 80,
            'tier': 'fast',
            'languages': 'English',
            'description': 'Fast, lightweight. Good baseline for keyword-heavy searches.',
        },
        'paraphrase-multilingual-MiniLM-L12-v2': {
            'name': 'huggingface/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
            'version': '1.0.1',
            'format': 'torch_script',
            'dimension': 384,
            'size_mb': 420,
            'tier': 'fast',
            'languages': 'Multilingual (50+)',
            'description': 'Fast multilingual model. 50+ languages with good quality.',
        },
        # Balanced tier - 768 dimensions
        'all-mpnet-base-v2': {
            'name': 'huggingface/sentence-transformers/all-mpnet-base-v2',
            'version': '1.0.1',
            'format': 'torch_script',
            'dimension': 768,
            'size_mb': 420,
            'tier': 'balanced',
            'languages': 'English',
            'description': 'Better semantic understanding. Good balance of speed and quality.',
        },
        'all-MiniLM-L12-v2': {
            'name': 'huggingface/sentence-transformers/all-MiniLM-L12-v2',
            'version': '1.0.1',
            'format': 'torch_script',
            'dimension': 384,
            'size_mb': 120,
            'tier': 'fast',
            'languages': 'English',
            'description': 'The default MiniLM with 12 layers. Same 384 dims, better quality.',
        },
        'multi-qa-MiniLM-L6-cos-v1': {
            'name': 'huggingface/sentence-transformers/multi-qa-MiniLM-L6-cos-v1',
            'version': '1.0.1',
            'format': 'torch_script',
            'dimension': 384,
            'size_mb': 80,
            'tier': 'fast',
            'languages': 'English',
            'description': 'Tuned for semantic search. Same 384 dims as the default.',
        },
        # Best quality tier
        'all-distilroberta-v1': {
            'name': 'huggingface/sentence-transformers/all-distilroberta-v1',
            'version': '1.0.1',
            'format': 'torch_script',
            'dimension': 768,
            'size_mb': 290,
            'tier': 'best',
            'languages': 'English',
            'description': 'Best retrieval quality for English.',
        },
        'distiluse-base-multilingual-cased-v1': {
            'name': 'huggingface/sentence-transformers/distiluse-base-multilingual-cased-v1',
            'version': '1.0.1',
            'format': 'torch_script',
            'dimension': 512,
            'size_mb': 480,
            'tier': 'best',
            'languages': 'Multilingual (15)',
            'description': 'Best quality for common languages (15 languages).',
        },
    }

    # Determine which models to download
    models_to_download: list[dict[str, str | int]] = []

    # Check for OPENSEARCH_MODELS environment variable (comma-separated short names)
    selected_models = os.environ.get('OPENSEARCH_MODELS', '').strip()
    download_all = os.environ.get('DOWNLOAD_ALL_OPENSEARCH_MODELS', 'false').lower() == 'true'

    if download_all:
        # Download all available models
        models_to_download = list(all_available_models.values())
        print_info(f'Downloading ALL {len(models_to_download)} OpenSearch neural models')
        print_info('(DOWNLOAD_ALL_OPENSEARCH_MODELS=true)')
    elif selected_models:
        # User specified specific models
        model_names = [m.strip() for m in selected_models.split(',') if m.strip()]
        print_info(f'Selected models: {", ".join(model_names)}')

        for short_name in model_names:
            if short_name in all_available_models:
                models_to_download.append(all_available_models[short_name])
            else:
                print_error(f'Unknown model: {short_name}')
                print_info(f'Available models: {", ".join(all_available_models.keys())}')

        if not models_to_download:
            print_error('No valid models specified. Using default.')
            models_to_download = [all_available_models['all-MiniLM-L6-v2']]
    else:
        # Default: download only the default model
        models_to_download = [all_available_models['all-MiniLM-L6-v2']]
        print_info('Downloading default OpenSearch model (all-MiniLM-L6-v2)')
        print_info('')
        print_info('To download additional models, set OPENSEARCH_MODELS:')
        print_info('  OPENSEARCH_MODELS="all-MiniLM-L6-v2,all-mpnet-base-v2"')
        print_info('')
        print_info('Available models:')
        for short_name, info in all_available_models.items():
            print_info(f'  {short_name} ({info["tier"]}, {info["languages"]}, {info["size_mb"]}MB)')

    print_info(f'\nDownloading {len(models_to_download)} model(s)...')

    # Create output directory
    output_dir = Path.home() / '.cache' / 'opensearch-ml'
    output_dir.mkdir(parents=True, exist_ok=True)

    downloaded_models = []
    failed_models: list[str] = []

    for model_info in models_to_download:
        model_name = str(model_info['name'])
        version = model_info['version']
        model_format = model_info['format']

        # Build the artifact URL
        # Format: https://artifacts.opensearch.org/models/ml-models/{name}/{version}/{format}/{filename}.zip
        # Example: huggingface/sentence-transformers/all-MiniLM-L6-v2 -> sentence-transformers_all-MiniLM-L6-v2
        name_parts = model_name.split('/')
        if len(name_parts) >= 3:
            # huggingface/sentence-transformers/all-MiniLM-L6-v2
            model_short_name = f'{name_parts[1]}_{name_parts[2]}'
        else:
            model_short_name = model_name.replace('/', '_')

        filename = f'{model_short_name}-{version}-{model_format}.zip'
        base_url = (
            f'https://artifacts.opensearch.org/models/ml-models/'
            f'{model_name}/{version}/{model_format}'
        )
        url = f'{base_url}/{filename}'
        # OpenSearch publishes a config.json beside every artifact. It carries the
        # exact `model_config` (model_type / framework_type / embedding_dimension /
        # all_config), the `model_content_hash_value`, and the true content size.
        #
        # Registering a model from a `file://` URL REQUIRES model_config — without it
        # OpenSearch 3.4 answers `400 illegal_argument_exception: model config is null`,
        # which is exactly how offline registration was broken. Fetching this file is
        # what makes the offline path work without hardcoding a per-model table of
        # model types that would drift from upstream (and be a guess).
        config_url = f'{base_url}/config.json'

        # Output path - use model short name as directory
        model_dir = (
            output_dir / name_parts[-1] if len(name_parts) >= 1 else output_dir / model_short_name
        )
        model_dir.mkdir(parents=True, exist_ok=True)
        output_path = model_dir / filename

        print_info(f'Downloading: {model_name}')
        print_info(f'  URL: {url}')
        print_info(f'  Output: {output_path}')

        try:
            import urllib.error
            import urllib.request

            # Check if already downloaded
            config_path = model_dir / 'config.json'

            if output_path.exists():
                print_success('  Already exists, skipping download')
                # An older cache holds the zip but no config.json, and the zip alone
                # cannot be registered offline. Fetch the missing half rather than
                # reporting a complete cache.
                has_config = _fetch_model_config(config_url, config_path)
                downloaded_models.append(
                    {
                        'name': model_name,
                        'path': str(output_path),
                        'config_path': str(config_path) if has_config else None,
                        'dimension': model_info['dimension'],
                        'version': version,
                        'format': model_format,
                    }
                )
                continue

            # Download the model
            print_info('  Downloading...')
            urllib.request.urlretrieve(url, output_path)

            # Verify file exists and has content
            if output_path.exists() and output_path.stat().st_size > 0:
                size_mb = round(output_path.stat().st_size / (1024 * 1024), 1)
                print_success(f'  Downloaded successfully ({size_mb} MB)')
                has_config = _fetch_model_config(config_url, config_path)
                downloaded_models.append(
                    {
                        'name': model_name,
                        'path': str(output_path),
                        'config_path': str(config_path) if has_config else None,
                        'dimension': model_info['dimension'],
                        'version': version,
                        'format': model_format,
                    }
                )
            else:
                print_error('  Download failed - file is empty or missing')
                failed_models.append(model_name)

        except urllib.error.HTTPError as e:
            print_error(f'  HTTP Error {e.code}: {e.reason}')
            print_error('  URL may not be available from OpenSearch artifacts')
            failed_models.append(model_name)
        except OSError as e:  # URLError, ContentTooShortError, disk write errors
            logger.warning('download of %s failed', model_name, exc_info=True)
            print_error(f'  Failed to download: {e}')
            failed_models.append(model_name)

    # Create manifest file
    manifest_path = output_dir / 'model_manifest.json'
    manifest = {
        'downloaded_at': datetime.now().isoformat(),
        'models': downloaded_models,
        'failed': failed_models,
    }

    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)

    print_info(f'Manifest saved to: {manifest_path}')

    if failed_models:
        print_warning(
            f'Failed to download {len(failed_models)} model(s): {", ".join(failed_models)}'
        )
        return {
            'opensearch_neural': {
                'status': 'partial',
                'downloaded': len(downloaded_models),
                'failed': failed_models,
                'path': str(output_dir),
            }
        }

    print_success(
        f'All {len(downloaded_models)} OpenSearch neural model(s) downloaded successfully'
    )
    return {
        'opensearch_neural': {
            'status': 'downloaded',
            'models': downloaded_models,
            'path': str(output_dir),
        }
    }
