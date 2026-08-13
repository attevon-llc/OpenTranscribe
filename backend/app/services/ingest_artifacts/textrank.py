"""TextRank over a TF-IDF sentence-similarity matrix. No model load, no LLM, no new deps.

This is the ranking core behind the extractive digest. It uses only ``numpy`` and
``nltk``, both already hard requirements, so it runs on a CPU worker in an air-gapped
deployment with ``LLM_PROVIDER`` empty — which is the whole point of the no-LLM tier
(#403 **D6**).

**Determinism is a correctness property here, not a nicety.** This output feeds Stage 3's
index and therefore every before/after retrieval measurement in the epic; a digest that
varies run to run injects noise into every delta. Three specific things buy it:

- the vocabulary is an explicitly **sorted** list, never ``set`` iteration order
  (``PYTHONHASHSEED`` is unpinned, so string set order differs per worker process);
- power iteration runs to a fixed tolerance with a fixed iteration cap and a uniform
  start vector — no random restart;
- ties are broken by source position, never by ``argsort`` on scores alone.

*Rejected alternatives*, recorded so they are not re-proposed: reading the 384-float
chunk vectors back out of OpenSearch (couples digest generation to index availability
**and** to whichever embedding model is deployed — re-embedding would silently change
every digest), and loading ``sentence-transformers`` client-side (a second copy of the
model to keep in sync, and it breaks offline installs).
"""

from __future__ import annotations

import logging
import re

import numpy as np

logger = logging.getLogger(__name__)

#: ISO 639-1 → NLTK Snowball stemmer language. Snowball's set overlaps punkt's but is not
#: identical (no Estonian, no Slovene, no Greek, no Czech), so this is its own map rather
#: than a reuse of ``search/chunking_service._PUNKT_LANG_MAP``.
_SNOWBALL_LANG_MAP: dict[str, str] = {
    "ar": "arabic",
    "da": "danish",
    "nl": "dutch",
    "en": "english",
    "fi": "finnish",
    "fr": "french",
    "de": "german",
    "hu": "hungarian",
    "it": "italian",
    "no": "norwegian",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "es": "spanish",
    "sv": "swedish",
}

#: ISO 639-1 → NLTK stopword corpus name.
_STOPWORD_LANG_MAP: dict[str, str] = dict(_SNOWBALL_LANG_MAP, el="greek", tr="turkish")

#: Word characters plus intra-word apostrophes; digits kept ("Q3", "2026").
_TOKEN_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?|\d+(?:[.,]\d+)*", re.UNICODE)

_stemmer_cache: dict[str, object] = {}
_stopword_cache: dict[str, frozenset[str]] = {}


def stopwords_for(language: str) -> frozenset[str]:
    """NLTK stopwords for *language* plus transcript filler, English as the fallback.

    Returns an empty set rather than raising when NLTK's corpus is unavailable: a digest
    with stopwords in its TF-IDF is worse, not broken, and an air-gapped install that
    never fetched the corpus must still get a digest (the Stage 2 gate is 100%).
    """
    if language in _stopword_cache:
        return _stopword_cache[language]

    from app.utils.text_preprocessing import TRANSCRIPT_FILLER

    words: set[str] = set()
    corpus_name = _STOPWORD_LANG_MAP.get(language, "english")
    try:
        from nltk.corpus import stopwords as nltk_stopwords

        words = set(nltk_stopwords.words(corpus_name))
    except Exception as exc:  # noqa: BLE001 - corpus missing / not downloadable
        logger.debug("NLTK stopwords unavailable for %r (%s); continuing without", language, exc)

    if corpus_name == "english":
        words |= set(TRANSCRIPT_FILLER)

    resolved = frozenset(words)
    _stopword_cache[language] = resolved
    return resolved


def _stemmer(language: str):
    """Snowball stemmer for *language*, or ``None`` when Snowball has no such language."""
    if language in _stemmer_cache:
        return _stemmer_cache[language]
    stemmer = None
    snowball_name = _SNOWBALL_LANG_MAP.get(language)
    if snowball_name:
        try:
            from nltk.stem.snowball import SnowballStemmer

            stemmer = SnowballStemmer(snowball_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Snowball stemmer unavailable for %r (%s)", language, exc)
    _stemmer_cache[language] = stemmer
    return stemmer


def tokenize(text: str, language: str = "en", *, stem: bool = True) -> list[str]:
    """Lowercase, stopword-filtered, optionally stemmed content tokens.

    Args:
        text: Raw sentence text.
        language: ISO 639-1 code.
        stem: Apply the Snowball stemmer. Off for keyphrase surface forms, on for
            similarity scoring, where "budget"/"budgets" must be the same feature.

    Returns:
        Tokens in source order. Order is preserved (not deduplicated) because term
        frequency is what TF-IDF weights.
    """
    stops = stopwords_for(language)
    tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
    tokens = [t for t in tokens if len(t) > 1 and t not in stops]
    if not stem:
        return tokens
    stemmer = _stemmer(language)
    if stemmer is None:
        return tokens
    return [str(stemmer.stem(t)) for t in tokens]


def tfidf_matrix(documents: list[list[str]]) -> np.ndarray:
    """L2-normalised TF-IDF matrix, one row per document.

    Args:
        documents: Tokenised documents (here: sentences).

    Returns:
        ``(n_documents, n_vocab)`` float64 array. Empty documents give all-zero rows,
        which the similarity step handles as "similar to nothing".
    """
    if not documents:
        return np.zeros((0, 0), dtype=np.float64)

    # sorted(), not set iteration: PYTHONHASHSEED is unpinned in the workers, so set
    # order for strings differs per process and the column layout — and therefore the
    # floating-point sums — would differ run to run.
    vocabulary = sorted({token for doc in documents for token in doc})
    if not vocabulary:
        return np.zeros((len(documents), 0), dtype=np.float64)
    index = {token: i for i, token in enumerate(vocabulary)}

    counts = np.zeros((len(documents), len(vocabulary)), dtype=np.float64)
    for row, doc in enumerate(documents):
        for token in doc:
            counts[row, index[token]] += 1.0

    n_docs = len(documents)
    document_frequency = np.count_nonzero(counts, axis=0)
    # Smoothed IDF, the scikit-learn spelling, so a term present in every sentence keeps
    # a small non-zero weight instead of annihilating the row.
    idf = np.log((1.0 + n_docs) / (1.0 + document_frequency)) + 1.0

    tfidf = counts * idf
    norms = np.linalg.norm(tfidf, axis=1, keepdims=True)
    np.divide(tfidf, norms, out=tfidf, where=norms > 0)
    return np.asarray(tfidf, dtype=np.float64)


def similarity_matrix(tfidf: np.ndarray) -> np.ndarray:
    """Cosine similarity between rows, self-similarity zeroed.

    Rows are already L2-normalised, so the dot product *is* the cosine. Negative values
    are impossible (TF-IDF is non-negative), so no clipping is needed.
    """
    if tfidf.size == 0:
        return np.zeros((tfidf.shape[0], tfidf.shape[0]), dtype=np.float64)
    similarity = np.asarray(tfidf @ tfidf.T, dtype=np.float64)
    np.fill_diagonal(similarity, 0.0)
    return similarity


def textrank(
    similarity: np.ndarray,
    *,
    damping: float = 0.85,
    max_iterations: int = 200,
    tolerance: float = 1e-10,
) -> np.ndarray:
    """PageRank over a sentence-similarity graph.

    Args:
        similarity: Square non-negative matrix with a zero diagonal.
        damping: Standard PageRank damping factor.
        max_iterations: Hard cap; convergence is typically <50 iterations.
        tolerance: L1 change below which iteration stops.

    Returns:
        A rank vector summing to 1. A sentence disconnected from every other (all-zero
        row — e.g. one made entirely of stopwords) keeps the uniform teleport mass
        rather than dropping to zero, so it can still be selected when it is all there is.
    """
    n = similarity.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    if n == 1:
        return np.ones(1, dtype=np.float64)

    row_sums = similarity.sum(axis=1, keepdims=True)
    transition = np.divide(similarity, row_sums, out=np.zeros_like(similarity), where=row_sums > 0)
    # Dangling rows redistribute uniformly, the textbook fix; without it their mass
    # leaks out of the system and the ranks stop summing to 1.
    dangling = row_sums.ravel() == 0.0
    if dangling.any():
        transition[dangling, :] = 1.0 / n

    rank = np.full(n, 1.0 / n, dtype=np.float64)
    teleport = (1.0 - damping) / n
    for _ in range(max_iterations):
        updated = teleport + damping * (transition.T @ rank)
        if np.abs(updated - rank).sum() < tolerance:
            rank = updated
            break
        rank = updated

    total = rank.sum()
    return rank / total if total > 0 else np.full(n, 1.0 / n, dtype=np.float64)


def rank_sentences(sentences: list[str], language: str = "en") -> np.ndarray:
    """Convenience: tokenise, build TF-IDF, and TextRank in one call."""
    return textrank(similarity_matrix(tfidf_matrix([tokenize(s, language) for s in sentences])))
