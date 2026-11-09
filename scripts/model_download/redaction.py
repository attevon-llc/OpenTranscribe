"""The content-redaction model download group."""

import os

from model_download.ui import print_error, print_header, print_info, print_success


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

    models: list[str] = []
    results: dict[str, str | list[str]] = {'status': 'downloaded', 'models': models}

    # GLiNER PII model
    try:
        from gliner import GLiNER

        gliner_model = 'knowledgator/gliner-pii-base-v1.0'
        print_info(f'Downloading GLiNER PII model: {gliner_model}')
        GLiNER.from_pretrained(gliner_model)
        print_success(f'  Downloaded: {gliner_model}')
        models.append(gliner_model)
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
                models.append(tox_model)
            except Exception as e:
                print_error(f'  Failed toxicity model {tox_model}: {e}')
                results['status'] = 'partial'
    except Exception as e:
        print_error(f'  transformers unavailable for toxicity models: {e}')
        results['status'] = 'partial'

    return {'redaction': results}
