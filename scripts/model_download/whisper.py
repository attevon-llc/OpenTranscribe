"""The WhisperX (local ASR) model download group."""

import os

from model_download.ui import print_error, print_header, print_info, print_success


def download_whisperx_models():
    """Download the local Whisper model named by ``WHISPER_MODEL`` from the model catalog.

    The catalog (``app.core.local_whisper_catalog``) is the one list of models a deployment can
    download and select, and ``app.services.local_models.downloader`` is the one downloader the
    Settings page uses too. A name outside the catalog is refused rather than downloaded.
    """
    print_header('Downloading Local Whisper Model')

    try:
        from app.core.local_whisper_catalog import (
            LOCAL_WHISPER_MODELS,
            canonical_id,
            get,
        )
        from app.services.local_models.downloader import (
            ERROR_MESSAGES,
            classify_error,
            download_blocking,
        )
    except ImportError:
        print_error(
            'This downloader image predates the model catalog; use the image matching this '
            "script's release (OT_IMAGE_TAG)."
        )
        return {'whisperx': {'status': 'failed', 'error': 'downloader_image_outdated'}}

    requested = os.environ.get('WHISPER_MODEL', 'large-v3-turbo')
    model_id = canonical_id(requested)
    entry = get(model_id) if model_id else None
    if entry is None:
        print_error(f"'{requested}' is not a supported local model.")
        print_info('Supported models: ' + ', '.join(m.id for m in LOCAL_WHISPER_MODELS))
        return {'whisperx': {'status': 'failed', 'error': 'unknown_local_model'}}

    print_info(f'Model: {entry.id} ({entry.repo_id})')
    try:
        download_blocking(entry, os.environ.get('HUGGINGFACE_TOKEN'))
    except Exception as exc:
        code = classify_error(exc)
        print_error(f'Failed to download local Whisper model: {ERROR_MESSAGES[code]}')
        return {'whisperx': {'status': 'failed', 'error': code}}

    print_success(f"Local Whisper model '{entry.id}' downloaded successfully")
    return {'whisperx': {'model': entry.id, 'repo_id': entry.repo_id, 'status': 'downloaded'}}
