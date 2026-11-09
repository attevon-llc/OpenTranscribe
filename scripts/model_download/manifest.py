"""The cache report and ``model_manifest.json`` written after a run."""

import json
import os
from datetime import datetime
from pathlib import Path

from model_download.ui import print_header, print_info, print_success


def get_cache_info():
    """Get information about cached models"""
    # Use default paths (same as backend)
    hf_home = str(Path.home() / '.cache' / 'huggingface')
    torch_home = str(Path.home() / '.cache' / 'torch')
    nltk_home = os.environ.get('NLTK_DATA') or str(Path.home() / '.cache' / 'nltk_data')
    sent_home = str(Path.home() / '.cache' / 'sentence-transformers')
    opensearch_ml_home = str(Path.home() / '.cache' / 'opensearch-ml')
    # Not under ~/.cache like the others -- /models (DIAR_MODELS_DIR) is the same
    # top-level mount point the backend and the diar-native sidecar both read from.
    diar_native_home = os.environ.get('DIAR_MODELS_DIR', '/models')

    cache_dirs = {
        'huggingface': Path(hf_home),
        'torch': Path(torch_home),
        'nltk_data': Path(nltk_home),
        'sentence_transformers': Path(sent_home),
        'opensearch_ml': Path(opensearch_ml_home),
        'diar_native': Path(diar_native_home),
    }

    info: dict[str, dict[str, object] | float] = {}
    total_size = 0

    for name, path in cache_dirs.items():
        if path.exists():
            size = sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
            total_size += size
            info[name] = {
                'path': str(path),
                'size_bytes': size,
                'size_gb': round(size / (1024**3), 2),
            }
        else:
            info[name] = {'path': str(path), 'exists': False}

    info['total_size_gb'] = round(total_size / (1024**3), 2)

    return info


def create_manifest(download_results):
    """Create manifest file with model information"""
    print_header('Creating Model Manifest')

    manifest = {
        'created_at': datetime.now().isoformat(),
        'models': download_results,
        'cache': get_cache_info(),
        'environment': {
            'whisper_model': os.environ.get('WHISPER_MODEL', 'large-v3-turbo'),
            'diarization_model': os.environ.get(
                'DIARIZATION_MODEL', 'pyannote/speaker-diarization-community-1'
            ),
            'use_gpu': os.environ.get('USE_GPU', 'true'),
            'compute_type': os.environ.get('COMPUTE_TYPE', 'float16'),
        },
    }

    # Write manifest to HF_HOME directory (inside the cache dir, not parent)
    cache_base = os.environ.get('HF_HOME', str(Path.home() / '.cache' / 'huggingface'))
    manifest_path = Path(cache_base) / 'model_manifest.json'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)

    print_success(f'Manifest created at: {manifest_path}')
    print_info(f'Total model cache size: {manifest["cache"]["total_size_gb"]} GB')

    return manifest
