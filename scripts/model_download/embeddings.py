"""Sentence-transformers, chat reranker and speaker-attribute model download groups."""

import logging
from pathlib import Path

from model_download.ui import print_error, print_header, print_info, print_success

logger = logging.getLogger(__name__)


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

    except Exception as e:  # group boundary: recorded as status=failed, download-models.py exits 1
        logger.exception('sentence-transformers download failed')
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

    except Exception as e:  # group boundary: recorded as status=failed, download-models.py exits 1
        logger.exception('chat reranker download failed')
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
        from transformers import (
            Wav2Vec2FeatureExtractor,
            Wav2Vec2ForSequenceClassification,
        )

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

    except Exception as e:  # group boundary: recorded as status=failed, download-models.py exits 1
        logger.exception('speaker attribute model download failed')
        print_error(f'Failed to download speaker attribute model: {e}')
        print_info('Speaker gender detection will not work offline.')
        print_info('This is non-critical - transcription will still function normally.')
        return {'speaker_attributes': {'status': 'failed', 'error': str(e)}}
