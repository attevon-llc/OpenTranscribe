"""The ``--only`` selector registry: group name -> downloader."""

from model_download.diar_native import download_diar_native_models
from model_download.embeddings import (
    download_chat_reranker,
    download_sentence_transformers,
    download_speaker_attribute_models,
)
from model_download.nltk import download_nltk_data
from model_download.opensearch import download_opensearch_neural_models
from model_download.pyannote import download_pyannote_models
from model_download.redaction import download_redaction_models
from model_download.whisper import download_whisperx_models

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
    'diar-native': download_diar_native_models,
}


#: Groups that need no Hugging Face credential. NLTK corpora come from NLTK's own
#: CDN, so requiring the token would make the airgap prefetch this selector exists
#: for fail on a deployment that has not configured one yet. The OpenSearch neural
#: models come from artifacts.opensearch.org, and ensure_opensearch_models runs
#: `--only opensearch` on installs that may have no token.
GROUPS_WITHOUT_HF_TOKEN = frozenset({'nltk', 'opensearch'})
