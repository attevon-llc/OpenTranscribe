#!/usr/bin/env python3
"""
Download all required AI models for OpenTranscribe offline packaging.

This script downloads:
- WhisperX models
- PyAnnote speaker diarization models
- NLTK data files (punkt_tab tokenizer)
- Sentence-Transformers models (all-MiniLM-L6-v2)
- wav2vec2 gender classifier (speaker attribute detection)

Models are cached to standard locations and a manifest is created.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# PyTorch 2.6+ compatibility fix - MUST be done BEFORE any ML library imports
# Patch torch.load to default to weights_only=False for trusted HuggingFace models
# This mirrors the fix in backend/app/core/celery.py (added by Wes Brown)
try:
    import torch

    _original_torch_load = torch.load

    def _patched_torch_load(*args, **kwargs):
        # Handle both missing weights_only AND weights_only=None (which PyTorch 2.8 treats as True)
        if kwargs.get('weights_only') is None:
            kwargs['weights_only'] = False
        return _original_torch_load(*args, **kwargs)

    torch.load = _patched_torch_load
    print('ℹ️  PyTorch 2.6+ compatibility: Patched torch.load for weights_only=False')
except ImportError:
    # torch not available yet, will be imported later
    pass


def print_header(text):
    """Print formatted header"""
    print(f'\n{"=" * 60}')
    print(f'  {text}')
    print(f'{"=" * 60}\n')


def print_info(text):
    """Print info message"""
    print(f'ℹ️  {text}')


def print_success(text):
    """Print success message"""
    print(f'✅ {text}')


def print_error(text):
    """Print error message"""
    print(f'❌ {text}')


def print_warning(text):
    """Print warning message"""
    print(f'⚠️  {text}')


def download_whisperx_models():
    """Download WhisperX models"""
    print_header('Downloading WhisperX Models')

    try:
        import whisperx

        model_name = os.environ.get('WHISPER_MODEL', 'large-v3-turbo')
        device = 'cuda' if os.environ.get('USE_GPU', 'true').lower() == 'true' else 'cpu'
        compute_type = os.environ.get('COMPUTE_TYPE', 'float16')

        print_info(f'Model: {model_name}')
        print_info(f'Device: {device}')
        print_info(f'Compute type: {compute_type}')

        print_info('Loading WhisperX model (this will download if needed)...')
        model = whisperx.load_model(model_name, device=device, compute_type=compute_type)

        print_success(f"WhisperX model '{model_name}' downloaded successfully")

        # Clean up
        del model

        return {
            'whisperx': {
                'model': model_name,
                'device': device,
                'compute_type': compute_type,
                'status': 'downloaded',
            }
        }

    except Exception as e:
        print_error(f'Failed to download WhisperX models: {e}')
        return {'whisperx': {'status': 'failed', 'error': str(e)}}


def validate_gated_model_access():
    """Validate access to gated PyAnnote models before attempting download"""
    print_info('Validating access to gated PyAnnote models...')
    print_info('')

    try:
        from huggingface_hub import HfApi

        hf_token = os.environ.get('HUGGINGFACE_TOKEN')
        if not hf_token:
            return False, 'No HuggingFace token provided'

        # List of REQUIRED gated models (community-1 is not gated)
        gated_models = [
            'pyannote/segmentation-3.0',
        ]

        api = HfApi()
        inaccessible_models = []
        access_errors = {}

        for model_id in gated_models:
            try:
                # Try to get model info - this will fail if user hasn't accepted the license
                api.model_info(model_id, token=hf_token)
                print_success(f'  ✓ Access confirmed: {model_id}')
            except Exception as e:
                error_msg = str(e)
                inaccessible_models.append(model_id)
                access_errors[model_id] = error_msg

                # Determine error type
                if '401' in error_msg or 'Unauthorized' in error_msg:
                    print_error(f'  ✗ Access DENIED (401 Unauthorized): {model_id}')
                    print_error('     You have NOT accepted the model agreement!')
                elif '403' in error_msg or 'Forbidden' in error_msg:
                    print_error(f'  ✗ Access DENIED (403 Forbidden): {model_id}')
                    print_error('     Token may not have required permissions')
                else:
                    print_error(f'  ✗ Cannot access: {model_id}')
                    print_error(f'     Error: {error_msg[:80]}')

        print_info('')

        if inaccessible_models:
            error_msg = f'Cannot access {len(inaccessible_models)} required gated model(s)'
            print_error('VALIDATION FAILED: Missing gated model access!')
            return False, (error_msg, access_errors)

        print_success('✅ All required gated models are accessible!')
        print_info('')
        return True, None

    except ImportError:
        # huggingface_hub might not be available, skip validation but warn
        print_info('  ⚠️  Skipping gated model validation (huggingface_hub not available)')
        print_info('  This is not recommended - validation may fail during download')
        print_info('')
        return True, None
    except Exception as e:
        # Unexpected error during validation - don't block but warn strongly
        print_error(f'  ⚠️  Could not validate gated models: {e}')
        print_info('  Proceeding anyway, but download may fail...')
        print_info('')
        return True, None


def validate_pyannote_download():
    """Validate that all expected PyAnnote models were downloaded to the torch cache"""
    torch_cache = Path.home() / '.cache' / 'torch' / 'pyannote'

    # Expected PyAnnote model directories
    expected_models = {
        'segmentation-3.0': 'models--pyannote--segmentation-3.0',
        'speaker-diarization-community-1': 'models--pyannote--speaker-diarization-community-1',
        'wespeaker-voxceleb': 'models--pyannote--wespeaker-voxceleb-resnet34-LM',
    }

    validation_result = {'all_present': True, 'models': {}}

    if not torch_cache.exists():
        validation_result['all_present'] = False
        for model_name in expected_models:
            validation_result['models'][model_name] = False
        return validation_result

    for model_name, model_dir in expected_models.items():
        model_path = torch_cache / model_dir
        is_present = model_path.exists() and model_path.is_dir()
        validation_result['models'][model_name] = is_present
        if not is_present:
            validation_result['all_present'] = False

    return validation_result


def download_pyannote_models():
    """Download PyAnnote models by running full WhisperX pipeline (same as backend)"""
    print_header('Downloading PyAnnote Models')

    try:
        import logging
        import warnings

        import numpy as np
        import torch
        import whisperx

        # Suppress noisy-but-harmless library warnings that confuse users:
        # - Lightning checkpoint auto-upgrade notice (cosmetic, not an error)
        # - PyAnnote TF32 reproducibility advisory (we accept the trade-off)
        # - WhisperX / transformers INFO logs during model load
        warnings.filterwarnings('ignore', message='.*Lightning automatically upgraded.*')
        warnings.filterwarnings('ignore', category=UserWarning, module='pyannote')
        logging.getLogger('whisperx').setLevel(logging.WARNING)
        logging.getLogger('pyannote').setLevel(logging.WARNING)
        logging.getLogger('lightning').setLevel(logging.ERROR)

        hf_token = os.environ.get('HUGGINGFACE_TOKEN')
        if not hf_token:
            print_error('HUGGINGFACE_TOKEN not set!')
            print_error('')
            print_error('A HuggingFace token is REQUIRED for speaker diarization models.')
            print_error('')
            print_error('To get your FREE token:')
            print_error('  1. Visit: https://huggingface.co/settings/tokens')
            print_error("  2. Click 'New token' and select 'Read' permissions")
            print_error('  3. Copy the token')
            print_error('')
            print_error('Then set it as an environment variable:')
            print_error('  export HUGGINGFACE_TOKEN=your_token_here')
            return {'pyannote': {'status': 'failed', 'error': 'No HuggingFace token'}}

        # Validate access to gated models BEFORE attempting download
        has_access, access_result = validate_gated_model_access()
        if not has_access:
            print_error('')
            print_error('=' * 80)
            print_error('❌ GATED MODEL ACCESS DENIED - DOWNLOAD CANNOT PROCEED')
            print_error('=' * 80)
            print_error('')
            print_error('⚠️  YOUR TOKEN DOES NOT HAVE ACCESS TO REQUIRED PYANNOTE MODELS')
            print_error('')
            print_error('This means you have NOT accepted the model user agreements.')
            print_error('')
            print_error('╔════════════════════════════════════════════════════════════════════╗')
            print_error('║  REQUIRED ACTION: Accept BOTH model agreements on HuggingFace      ║')
            print_error('╚════════════════════════════════════════════════════════════════════╝')
            print_error('')
            print_error('Step 1: Visit the Segmentation Model page')
            print_error('   URL: https://huggingface.co/pyannote/segmentation-3.0')
            print_error("   → Look for the 'Agree and access repository' button")
            print_error('   → Click it to accept the terms')
            print_error('')
            print_error('Step 2: Visit the Speaker Diarization Model page')
            print_error('   URL: https://huggingface.co/pyannote/speaker-diarization-community-1')
            print_error("   → Look for the 'Agree and access repository' button")
            print_error('   → Click it to accept the terms')
            print_error('')
            print_error('Step 3: Wait 1-2 minutes for permissions to propagate')
            print_error('')
            print_error('Step 4: Run this script again:')
            print_error('   bash scripts/download-models.sh models')
            print_error('')
            print_error('=' * 80)
            print_error('')

            error_msg = access_result[0] if isinstance(access_result, tuple) else access_result
            return {'pyannote': {'status': 'failed', 'error': error_msg}}

        # Use default paths (same as backend) - let WhisperX/PyAnnote handle caching
        print_info('Using WhisperX full pipeline (same as backend) to download all models')
        print_info('Models will be cached to default locations (managed by WhisperX/PyAnnote)')

        # Detect device
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        compute_type = 'float16' if device == 'cuda' else 'float32'
        print_info(f'Using device: {device}')

        # Create synthetic audio (30 seconds of silence with some noise for testing)
        print_info('Creating synthetic test audio (30 seconds)...')
        sample_rate = 16000
        duration = 30  # 30 seconds

        # Generate audio with some speech-like characteristics
        # Use a simple sine wave pattern that mimics speech frequencies
        t = np.linspace(0, duration, sample_rate * duration)
        frequencies = [300, 500, 700]  # Speech-like frequencies
        audio = np.zeros_like(t)
        for freq in frequencies:
            audio += 0.1 * np.sin(2 * np.pi * freq * t)

        # Add some noise to make it more realistic
        audio += 0.05 * np.random.randn(len(t))

        # Normalize
        audio = audio.astype(np.float32)
        print_success('  Synthetic audio created')

        # Use WhisperX full pipeline (same as backend)
        print_info('Step 1/3: Audio ready...')
        print_success('  Audio prepared')

        # Step 2: Transcribe (same as backend)
        print_info('Step 2/3: Running WhisperX transcription...')
        model = whisperx.load_model(
            os.environ.get('WHISPER_MODEL', 'base'), device=device, compute_type=compute_type
        )
        model.transcribe(audio, batch_size=16 if device == 'cuda' else 1)
        print_success('  Transcription completed')
        del model
        torch.cuda.empty_cache() if device == 'cuda' else None

        # Step 3: Diarize (same as backend - downloads PyAnnote models)
        print_info('Step 3/3: Running speaker diarization (downloads PyAnnote models)...')
        print_info('  This downloads: segmentation-3.0, embedding, wespeaker-voxceleb...')

        try:
            diarize_model = whisperx.diarize.DiarizationPipeline(
                use_auth_token=hf_token, device=device
            )
        except TypeError:
            # pyannote-audio 3.3+ renamed use_auth_token → token
            diarize_model = whisperx.diarize.DiarizationPipeline(token=hf_token, device=device)

        diarize_model(audio, min_speakers=1, max_speakers=20)

        print_success('  Diarization completed')
        print_success('  All PyAnnote model weights downloaded')

        # Clean up
        del diarize_model
        del audio
        torch.cuda.empty_cache() if device == 'cuda' else None

        # Diarization ran without exception — models are present and working.
        # (PyAnnote 3.x stores weights in ~/.cache/huggingface/hub/, not the
        #  old ~/.cache/torch/pyannote/ path, so directory-name checks are
        #  unreliable. A successful pipeline run is the definitive proof.)
        return {
            'pyannote': {
                'model': 'pyannote/speaker-diarization-community-1',
                'status': 'downloaded',
                'validation': {'all_present': True},
            }
        }

    except Exception as e:
        error_msg = str(e)
        print_error(f'Failed to download PyAnnote models: {error_msg}')

        # Check if this looks like a gated model access error
        if (
            'cannot find the requested files' in error_msg.lower()
            or 'locate the file on the hub' in error_msg.lower()
            or '403' in error_msg
            or '401' in error_msg
        ):
            print_error('')
            print_error('=' * 70)
            print_error('⚠️  THIS LOOKS LIKE A GATED MODEL ACCESS ERROR!')
            print_error('=' * 70)
            print_error('')
            print_error("This error usually means you haven't accepted the model agreements.")
            print_error('')
            print_error('You MUST accept BOTH PyAnnote gated model agreements:')
            print_error('')
            print_error('  1. Segmentation Model:')
            print_error('     https://huggingface.co/pyannote/segmentation-3.0')
            print_error("     → Click 'Agree and access repository'")
            print_error('')
            print_error('  2. Speaker Diarization Model:')
            print_error('     https://huggingface.co/pyannote/speaker-diarization-community-1')
            print_error("     → Click 'Agree and access repository' (if prompted)")
            print_error('')
            print_error('After accepting BOTH agreements:')
            print_error('  • Wait 1-2 minutes for permissions to propagate')
            print_error('  • Run this script again: bash scripts/download-models.sh models')
            print_error('')
            print_error('=' * 70)

        return {'pyannote': {'status': 'failed', 'error': str(e)}}


def download_nltk_data():
    """Download NLTK data files required by transformers/whisperx"""
    print_header('Downloading NLTK Data')

    try:
        import nltk

        # Honour $NLTK_DATA, which is what the images actually set (issue #491).
        #
        # This was hardcoded to `Path.home()/.cache/nltk_data`, and it matched the
        # image only by luck of $HOME: Dockerfile.prod and Dockerfile.lite run as
        # `appuser`, but Dockerfile.blackwell runs as `user` and sets
        # NLTK_DATA=/home/user/.cache/nltk_data. So on the Blackwell image this
        # fetched the corpora into a directory nothing reads, reported success,
        # and left the deployment to discover the gap at runtime.
        nltk_data_path = Path(os.environ.get('NLTK_DATA') or (Path.home() / '.cache' / 'nltk_data'))
        nltk_data_path.mkdir(parents=True, exist_ok=True)

        # Add to NLTK's data path
        if str(nltk_data_path) not in nltk.data.path:
            nltk.data.path.insert(0, str(nltk_data_path))

        print_info(f'NLTK data path: {nltk_data_path}')

        # Download required NLTK data packages
        required_packages = [
            'punkt_tab',  # Punkt tokenizer (new tabular format)
            'punkt',  # Punkt tokenizer (legacy format for compatibility)
            'averaged_perceptron_tagger_eng',  # POS tagger for NER
            'maxent_ne_chunker_tab',  # Named entity chunker for speaker name extraction
            'words',  # English word corpus (required by NE chunker)
            # Topic/keyword extraction: app/utils/text_preprocessing.py and
            # services/ingest_artifacts/textrank.py. Absent here, it was fetched at RUNTIME on
            # first topic extraction — which fails on an airgapped deployment (issue #491).
            'stopwords',
        ]

        downloaded = []
        for package in required_packages:
            try:
                print_info(f'Downloading NLTK package: {package}')
                nltk.download(package, download_dir=str(nltk_data_path), quiet=False)
                downloaded.append(package)
                print_success(f'  Downloaded: {package}')
            except Exception as pkg_error:
                print_error(f'  Failed to download {package}: {pkg_error}')

        if downloaded:
            print_success(f'NLTK data downloaded successfully ({len(downloaded)} packages)')
        else:
            raise Exception('No NLTK packages were downloaded')

        return {
            'nltk': {'packages': downloaded, 'path': str(nltk_data_path), 'status': 'downloaded'}
        }

    except Exception as e:
        print_error(f'Failed to download NLTK data: {e}')
        return {'nltk': {'status': 'failed', 'error': str(e)}}


def download_sentence_transformers():
    """Download sentence-transformers model for semantic search"""
    print_header('Downloading Sentence-Transformers Model')

    try:
        from sentence_transformers import SentenceTransformer

        model_name = 'all-MiniLM-L6-v2'
        cache_path = Path.home() / '.cache' / 'sentence-transformers'
        cache_path.mkdir(parents=True, exist_ok=True)

        print_info(f'Model: {model_name}')
        print_info(f'Cache path: {cache_path}')
        print_info('Loading sentence-transformers model (this will download if needed)...')

        # Load model (will download to cache if not present)
        model = SentenceTransformer(model_name, cache_folder=str(cache_path))

        # Test the model with a sample text to ensure it works
        test_embedding = model.encode('This is a test sentence.')
        print_info(f'  Embedding dimension: {len(test_embedding)}')

        print_success(f"Sentence-transformers model '{model_name}' downloaded successfully")

        # Clean up
        del model

        return {
            'sentence_transformers': {
                'model': model_name,
                'path': str(cache_path),
                'dimension': len(test_embedding),
                'status': 'downloaded',
            }
        }

    except Exception as e:
        print_error(f'Failed to download sentence-transformers model: {e}')
        return {'sentence_transformers': {'status': 'failed', 'error': str(e)}}


def download_chat_reranker():
    """Download the cross-encoder used to rerank RAG chat retrieval results.

    ~90MB, Apache 2.0. Loaded lazily on CPU in the backend container the first
    time someone chats; pre-downloading keeps that first message fast and makes
    offline/air-gapped deployments work. Chat degrades gracefully without it
    (reranking simply switches off), so a failure here is not fatal.
    """
    print_header('Downloading Chat Reranker Model')

    try:
        from sentence_transformers import CrossEncoder

        model_name = 'cross-encoder/ms-marco-MiniLM-L-6-v2'
        cache_path = Path.home() / '.cache' / 'huggingface'
        cache_path.mkdir(parents=True, exist_ok=True)

        print_info(f'Model: {model_name}')
        print_info(f'Cache path: {cache_path}')
        print_info('Loading cross-encoder (this will download if needed)...')

        model = CrossEncoder(model_name, device='cpu', max_length=512)

        # Score a trivial pair so a broken download surfaces here, not at runtime.
        scores = model.predict([('what was decided', 'we agreed to ship on Tuesday')])
        print_info(f'  Test relevance score: {float(scores[0]):.4f}')

        print_success(f"Chat reranker '{model_name}' downloaded successfully")

        del model

        return {
            'chat_reranker': {
                'model': model_name,
                'path': str(cache_path),
                'status': 'downloaded',
            }
        }

    except Exception as e:
        print_error(f'Failed to download chat reranker: {e}')
        return {'chat_reranker': {'status': 'failed', 'error': str(e)}}


def download_speaker_attribute_models():
    """Download wav2vec2 gender classifier for speaker attribute detection.

    Uses prithivMLmods/Common-Voice-Gender-Detection (~380MB, Apache 2.0) which
    is fine-tuned from wav2vec2-base-960h for gender classification (98.46% accuracy).
    Must be pre-downloaded for offline/air-gapped deployments.
    """
    print_header('Downloading Speaker Attribute Model (gender)')

    try:
        from transformers import Wav2Vec2FeatureExtractor, Wav2Vec2ForSequenceClassification

        model_name = 'prithivMLmods/Common-Voice-Gender-Detection'
        print_info(f'Model: {model_name}')
        print_info('Loading gender classifier model (this will download if needed)...')

        feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(model_name)
        model = Wav2Vec2ForSequenceClassification.from_pretrained(model_name)

        print_success(f"Gender model '{model_name}' downloaded successfully")

        # Clean up
        del feature_extractor, model

        return {
            'speaker_attributes': {
                'model': model_name,
                'status': 'downloaded',
            }
        }

    except Exception as e:
        print_error(f'Failed to download speaker attribute model: {e}')
        print_info('Speaker gender detection will not work offline.')
        print_info('This is non-critical - transcription will still function normally.')
        return {'speaker_attributes': {'status': 'failed', 'error': str(e)}}


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
    except Exception as exc:  # noqa: BLE001 - any failure means "no config", reported here
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

    # Complete registry of available models - matches OPENSEARCH_EMBEDDING_MODELS in constants.py
    all_available_models = {
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
    models_to_download = []

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
    failed_models = []

    for model_info in models_to_download:
        model_name = model_info['name']
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
        except Exception as e:
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


def download_redaction_models():
    """Download content-redaction models (PII + toxicity) for offline use.

    - GLiNER PII model (knowledgator/gliner-pii-base-v1.0, ~CPU, names/addresses)
    - Toxicity classifiers: Tiny-Toxic-Detector (English) + multilingual-toxic-xlm-roberta
    - Presidio's spaCy backbone (en_core_web_lg) is installed via the Dockerfile.

    Skipped unless DOWNLOAD_REDACTION_MODELS=true (default true in the prod image).
    Non-critical: redaction simply runs with fewer detectors if a model is missing.
    """
    print_header('Downloading Content Redaction Models (PII + toxicity)')

    if os.environ.get('DOWNLOAD_REDACTION_MODELS', 'true').lower() != 'true':
        print_info('Skipping redaction models (DOWNLOAD_REDACTION_MODELS != true)')
        return {'redaction': {'status': 'skipped'}}

    results = {'status': 'downloaded', 'models': []}

    # GLiNER PII model
    try:
        from gliner import GLiNER

        gliner_model = 'knowledgator/gliner-pii-base-v1.0'
        print_info(f'Downloading GLiNER PII model: {gliner_model}')
        GLiNER.from_pretrained(gliner_model)
        print_success(f'  Downloaded: {gliner_model}')
        results['models'].append(gliner_model)
    except Exception as e:
        print_error(f'  Failed GLiNER PII model: {e}')
        results['status'] = 'partial'

    # Toxicity models (via transformers AutoModel)
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        for tox_model in (
            'unitary/toxic-bert',
            'unitary/multilingual-toxic-xlm-roberta',
        ):
            try:
                print_info(f'Downloading toxicity model: {tox_model}')
                AutoTokenizer.from_pretrained(tox_model)
                AutoModelForSequenceClassification.from_pretrained(tox_model)
                print_success(f'  Downloaded: {tox_model}')
                results['models'].append(tox_model)
            except Exception as e:
                print_error(f'  Failed toxicity model {tox_model}: {e}')
                results['status'] = 'partial'
    except Exception as e:
        print_error(f'  transformers unavailable for toxicity models: {e}')
        results['status'] = 'partial'

    return {'redaction': results}


def get_cache_info():
    """Get information about cached models"""
    # Use default paths (same as backend)
    hf_home = str(Path.home() / '.cache' / 'huggingface')
    torch_home = str(Path.home() / '.cache' / 'torch')
    nltk_home = os.environ.get('NLTK_DATA') or str(Path.home() / '.cache' / 'nltk_data')
    sent_home = str(Path.home() / '.cache' / 'sentence-transformers')
    opensearch_ml_home = str(Path.home() / '.cache' / 'opensearch-ml')

    cache_dirs = {
        'huggingface': Path(hf_home),
        'torch': Path(torch_home),
        'nltk_data': Path(nltk_home),
        'sentence_transformers': Path(sent_home),
        'opensearch_ml': Path(opensearch_ml_home),
    }

    info = {}
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


#: `--only <group>` -> the downloader that fetches it.
#:
#: A selector exists because the one-shot containers in `scripts/common.sh` mount
#: ONE cache directory each (issue #491): whatever a run fetches outside the
#: mounted directory is written into the container's own filesystem and discarded
#: with it. Running the whole downloader to obtain one group therefore burns the
#: bandwidth and keeps none of it.
DOWNLOAD_GROUPS = {
    'whisperx': download_whisperx_models,
    'pyannote': download_pyannote_models,
    'nltk': download_nltk_data,
    'sentence-transformers': download_sentence_transformers,
    'reranker': download_chat_reranker,
    'speaker-attributes': download_speaker_attribute_models,
    'opensearch': download_opensearch_neural_models,
    'redaction': download_redaction_models,
}

#: Groups that need no Hugging Face credential. NLTK corpora come from NLTK's own
#: CDN, so requiring the token would make the airgap prefetch this selector exists
#: for fail on a deployment that has not configured one yet.
GROUPS_WITHOUT_HF_TOKEN = frozenset({'nltk'})


def main():
    """Main execution"""
    parser = argparse.ArgumentParser(description='Download OpenTranscribe model assets')
    parser.add_argument(
        '--only',
        action='append',
        choices=sorted(DOWNLOAD_GROUPS),
        help='Fetch only this group; repeatable. Default: every group.',
    )
    args = parser.parse_args()

    selected = args.only or list(DOWNLOAD_GROUPS)

    print_header('OpenTranscribe Model Downloader')
    if args.only:
        print_info(f'Selected group(s): {", ".join(selected)}')
    else:
        print_info('This script will download all required AI models')
    print_info('Models will be cached for offline packaging\n')

    # Check for required environment variables
    if not set(selected) <= GROUPS_WITHOUT_HF_TOKEN and not os.environ.get('HUGGINGFACE_TOKEN'):
        print_error('HUGGINGFACE_TOKEN environment variable not set!')
        print_info('Get your token at: https://huggingface.co/settings/tokens')
        sys.exit(1)

    results = {}

    for group in selected:
        results.update(DOWNLOAD_GROUPS[group]())

    # Create manifest
    manifest = create_manifest(results)

    # Summary
    print_header('Download Summary')

    failed = [k for k, v in results.items() if v.get('status') == 'failed']
    succeeded = [k for k, v in results.items() if v.get('status') == 'downloaded']

    if failed:
        print_error(f'Failed to download: {", ".join(failed)}')
        for model in failed:
            print_error(f'  {model}: {results[model].get("error", "Unknown error")}')
        sys.exit(1)

    print_success(f'Successfully downloaded {len(succeeded)} model(s)')
    print_success(f'Total cache size: {manifest["cache"]["total_size_gb"]} GB')

    print('\nCache locations:')
    for name, info in manifest['cache'].items():
        if name != 'total_size_gb':
            print(f'  {name}: {info["path"]} ({info.get("size_gb", 0)} GB)')

    print_success('\n✨ All models downloaded successfully!')


if __name__ == '__main__':
    main()
