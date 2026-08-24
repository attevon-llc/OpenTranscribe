"""Text preprocessing utilities for LLM-based topic extraction.

Uses NLTK for standard NLP preprocessing (tokenization, stopword removal)
with minimal domain-specific additions for transcript noise.

Pipeline: raw text → domain cleanup → NLTK word_tokenize → stopword filter → output
  - Tokenizer: NLTK word_tokenize (Penn Treebank standard)
  - Stopwords: NLTK English stopwords corpus + transcript filler words
  - No stemming/lemmatization: LLMs work better with natural word forms

IMPORTANT: Only use for topics/collections extraction. Do NOT use for:
- Summary generation (needs full grammar and sentence structure)
- Speaker identification (needs conversational patterns and names)
"""

import logging
import re
from functools import lru_cache

from app.utils.nltk_offline import NLTK_CORPUS_UNAVAILABLE
from app.utils.nltk_offline import nltk_downloads_permitted

logger = logging.getLogger(__name__)

# Transcript-specific filler words not in NLTK's standard English stopwords.
# These are speech disfluencies and verbal fillers common in spoken transcripts.
# Public: services/ingest_artifacts/textrank.py needs the same filler set for the
# extractive digest's TF-IDF, and a second copy would drift.
TRANSCRIPT_FILLER = frozenset(
    {
        "um",
        "uh",
        "uh-huh",
        "uhh",
        "umm",
        "hmm",
        "ah",
        "oh",
        "eh",
        "yeah",
        "yep",
        "yup",
        "nah",
        "okay",
        "ok",
        "gonna",
        "gotta",
        "wanna",
        "kinda",
        "sorta",
        "basically",
        "literally",
        "actually",
    }
)

# Penn Treebank contraction remnants produced by NLTK word_tokenize.
# e.g., "It's" → ["It", "'s"], "don't" → ["do", "n't"], "can't" → ["ca", "n't"]
# These are standard PTB tokenizer artifacts and carry no topical meaning.
# Includes irregular contraction stems: ca(n't), wo(n't), sha(n't).
_PTB_CONTRACTION_TOKENS = frozenset(
    {
        "'s",
        "'t",
        "'re",
        "'ve",
        "'ll",
        "'d",
        "'m",
        "n't",
        "\u2019s",
        "\u2019t",
        "\u2019re",
        "\u2019ve",
        "\u2019ll",
        "\u2019d",
        "\u2019m",
        "ca",
        "wo",
        "sha",  # Irregular PTB splits: can't→ca, won't→wo, shan't→sha
    }
)

# Minimum output ratio — if preprocessing is too aggressive, fall back to
# lightly-cleaned text. Protects non-English and highly numeric content.
_MIN_OUTPUT_RATIO = 0.10


@lru_cache(maxsize=1)
def _get_stopwords() -> frozenset[str]:
    """Load NLTK English stopwords, degrading rather than raising (issue #491).

    Uses lru_cache so the NLTK lookup happens only once per process.

    ⚠️ **The retry has to be guarded, and it was not.** The old shape caught
    ``LookupError``, called ``nltk.download("stopwords")``, and re-called
    ``stopwords.words("english")`` with **no second ``except``**. ``nltk.download``
    swallows its own network errors and returns falsy, so on an airgapped or
    firewalled deployment the retry raised ``LookupError`` straight out of
    :func:`preprocess_for_topics` and into ``topic_extraction_service`` — topic
    extraction failed outright on a deployment that had provisioned every model it
    was told to.

    Its neighbour :func:`_tokenize` has had the correct nested shape all along;
    this one simply never grew it. The fallback here is the empty set plus the
    transcript filler: stopwords only *improve* keyword extraction by removing
    noise words, so their absence degrades quality rather than producing a wrong
    answer, and a loud warning is the honest signal.

    Returns:
        The stopword set, or just :data:`TRANSCRIPT_FILLER` when the corpus is
        unavailable.
    """
    import nltk

    try:
        from nltk.corpus import stopwords

        words = set(stopwords.words("english"))
    except NLTK_CORPUS_UNAVAILABLE:
        # NLTK_CORPUS_UNAVAILABLE, not LookupError — an unreadable corpus raises
        # OSError. ⚠️ This function is @lru_cache(maxsize=1), so whichever answer
        # it produces is FROZEN for the life of the process: repairing the corpus
        # on disk needs a worker restart to take effect.
        try:
            if nltk_downloads_permitted(corpus="the stopwords corpus"):
                logger.info("Downloading NLTK stopwords corpus (one-time)")
                nltk.download("stopwords", quiet=True)
            from nltk.corpus import stopwords

            words = set(stopwords.words("english"))
        except Exception as exc:
            logger.warning(
                "NLTK stopwords unavailable (%s: %s); keyword extraction will keep "
                "common words, and this verdict is cached for the life of this "
                "process — restart the worker after provisioning. Run "
                "scripts/download-models.sh (or ./opentr.sh start) to provision the "
                "NLTK corpora — see docs-site/docs/installation/offline-installation.md.",
                type(exc).__name__,
                exc,
            )
            words = set()

    # Merge with transcript-specific filler
    return frozenset(words | TRANSCRIPT_FILLER)


def _tokenize(text: str) -> list[str]:
    """Tokenize text using NLTK word_tokenize with punkt fallback.

    Falls back to simple regex tokenization if NLTK data is unavailable
    (e.g., in offline/airgapped environments without punkt_tab).
    """
    import nltk

    try:
        return list(nltk.word_tokenize(text))
    except NLTK_CORPUS_UNAVAILABLE:
        # NLTK_CORPUS_UNAVAILABLE, not LookupError: an unreadable corpus (bad
        # ownership on the model cache, or nltk >=3.10 pathsec refusing a
        # multiply-linked file) raises OSError, which the narrower guard let
        # escape into the topic-extraction task. The regex fallback below is a
        # perfectly good answer for both.
        try:
            if nltk_downloads_permitted(corpus="the punkt_tab tokenizer"):
                logger.info("Downloading NLTK punkt_tab tokenizer (one-time)")
                nltk.download("punkt_tab", quiet=True)
            return list(nltk.word_tokenize(text))
        except Exception as exc:
            # Fallback: simple regex tokenizer if NLTK data unavailable
            logger.warning(
                "NLTK tokenizer unavailable (%s: %s), using regex fallback",
                type(exc).__name__,
                exc,
            )
            return list(re.findall(r"\b\w+(?:'\w+)?\b", text))


def preprocess_for_topics(text: str, max_chars: int = 0) -> str:
    """Preprocess transcript text for topic extraction.

    Uses NLTK word_tokenize for standard tokenization and NLTK English
    stopwords for filtering, supplemented with transcript-specific filler
    words. Preserves numbers, acronyms, and named entities.

    Does NOT stem words — LLMs work better with natural language tokens.

    Falls back to lightly-cleaned raw text if preprocessing removes more
    than 90% of content (protects non-English / highly numeric content).

    Args:
        text: Raw transcript text
        max_chars: Maximum output characters (0 = no limit)

    Returns:
        Preprocessed text optimized for topic extraction
    """
    if not text:
        return ""

    original_len = len(text)

    # --- Domain-specific cleanup (not NLP, just transcript format) ---

    # Remove speaker labels (SPEAKER_01:, Speaker 1:, etc.)
    cleaned = re.sub(r"\b(?:SPEAKER_?\d+|Speaker\s*\d+)\s*:", "", text, flags=re.IGNORECASE)

    # Remove timestamps [00:00], [00:00:00], (00:00), etc.
    cleaned = re.sub(r"[\[\(]\d{1,2}:\d{2}(?::\d{2})?[\]\)]", "", cleaned)

    # Normalize whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # --- Standard NLP pipeline ---

    stop_words = _get_stopwords()
    tokens = _tokenize(cleaned)

    # Filter: remove stopwords, PTB contraction remnants, punctuation-only tokens,
    # and single characters (except standalone digits: "5 million", "3 phases").
    filtered = [
        t.lower()
        for t in tokens
        if t.lower() not in stop_words
        and t not in _PTB_CONTRACTION_TOKENS
        and (t.isalnum() or any(c.isalnum() for c in t))
        and (len(t) >= 2 or t.isdigit())
    ]

    result = " ".join(filtered)

    # Safety fallback: if too much was removed, return the lightly-cleaned text
    # (speaker labels and timestamps removed, but stopwords preserved).
    # This protects non-English transcripts and heavily numeric content.
    if original_len > 100 and len(result) < original_len * _MIN_OUTPUT_RATIO:
        logger.warning(
            f"Preprocessing too aggressive ({len(result)}/{original_len} chars = "
            f"{len(result) * 100 // original_len}%), falling back to light cleanup"
        )
        result = cleaned

    if max_chars and len(result) > max_chars:
        # Truncate at word boundary
        result = result[:max_chars].rsplit(" ", 1)[0]

    return result
