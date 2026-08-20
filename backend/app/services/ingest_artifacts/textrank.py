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
import unicodedata

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


def _combining_mark_ranges(limit: int = 0x10000) -> str:
    """``\\uXXXX-\\uYYYY`` regex ranges covering every BMP codepoint whose Unicode
    general category is ``Mn`` (nonspacing mark) or ``Mc`` (spacing combining mark),
    collapsed into contiguous runs.

    ``[^\\W\\d_]`` — the class ``_TOKEN_RE`` used before this fix — is Python's word
    class minus digits and underscore, and a combining mark is not a member: Python
    classifies ``\\w`` from the same general-category table this function reads, and
    Mn/Mc are neither letters nor digits. So a base consonant followed by its own
    combining vowel or tone sign (Thai ``ก`` + ``ั``, Devanagari ``क`` + ``ि``) tokenized
    as two separate one-character matches — TextRank then scored fragments of a single
    grapheme as if they were independent words, corrupting both the digest and the
    keyphrase extraction for every language written this way.

    Computed once at import from ``unicodedata`` rather than hand-listing script
    blocks (contrast ``search/chunking_service._NO_SPACE_SCRIPT``, which deliberately
    IS a hand-picked script list for a different problem — scriptio-continua word
    counting): a general-category sweep covers every combining script, not only the
    two exercised by this fix's tests. Bounded to the BMP — combining marks in the
    supplementary planes are historic/rare scripts this app has no other support for,
    and scanning the full 0x110000 codepoint space would cost proportionally more at
    every import for no realistic gain.
    """
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for cp in range(limit):
        is_mark = unicodedata.category(chr(cp)) in ("Mn", "Mc")
        if is_mark and start is None:
            start = cp
        elif not is_mark and start is not None:
            ranges.append((start, cp - 1))
            start = None
    if start is not None:
        ranges.append((start, limit - 1))
    return "".join(f"\\u{a:04x}-\\u{b:04x}" if a != b else f"\\u{a:04x}" for a, b in ranges)


#: A "letter" for tokenizing purposes: a word character (minus digits/underscore) OR
#: a combining mark attached to one — see :func:`_combining_mark_ranges`. Alternation,
#: not a single negated bracket expression: unioning positive mark ranges INTO a
#: negated class (``[^\W\d_ั...]``) would EXCLUDE them instead of including them,
#: since a negated bracket expression matches "none of these", so the two pieces have
#: to be separate alternatives that are then unioned by `|`.
_MARK_RANGES = _combining_mark_ranges()
_LETTER = rf"(?:[^\W\d_]|[{_MARK_RANGES}])"

#: Word characters (letters plus their combining marks) plus intra-word apostrophes;
#: digits kept ("Q3", "2026").
_TOKEN_RE = re.compile(rf"{_LETTER}+(?:'{_LETTER}+)?|\d+(?:[.,]\d+)*", re.UNICODE)

#: A minimal English stopword list, used when NLTK's corpus is unavailable.
#:
#: ⚠️ Not belt-and-braces — without it, keyphrase extraction produces NOTHING on any
#: deployment that never fetched the NLTK corpus. `keyphrases.py` is RAKE-shaped: it
#: splits on stopwords to find candidate boundaries, so an EMPTY stopword set gives it
#: no boundaries at all, the whole text becomes one candidate, and that candidate then
#: exceeds `_MAX_PHRASE_WORDS` and is dropped. Zero phrases, no error, no log line.
#:
#: The empty-set fallback below is right for TextRank — a digest with stopwords left in
#: its TF-IDF is *worse, not broken* — and silently fatal for RAKE. One fallback served
#: two consumers with opposite tolerances. Caught by CI, where the corpus is absent.
#:
#: Every word here is a **strict subset** of NLTK's own English list, so the union is a
#: no-op wherever the corpus IS present and only the air-gapped path changes. That is a
#: requirement, not a coincidence, and it is asserted by
#: ``test_ingest_artifacts_facts.py``: a word NLTK does not stop would alter the digest's
#: TF-IDF on every existing deployment without a ``generator_version`` bump, i.e. produce
#: a mixed-vintage corpus measured as if it were one thing. ("also" and "would" were in a
#: first draft and were removed for exactly that reason.)
_FALLBACK_ENGLISH_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "between",
        "both",
        "but",
        "by",
        "can",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "you",
        "your",
        "yours",
    }
)

_stemmer_cache: dict[str, object] = {}
_stopword_cache: dict[str, frozenset[str]] = {}


def stopwords_for(language: str) -> frozenset[str]:
    """NLTK stopwords for *language* plus transcript filler, English as the fallback.

    Never raises when NLTK's corpus is unavailable: an air-gapped install that never
    fetched it must still get a digest (the Stage 2 gate is 100%), and a digest with
    stopwords left in its TF-IDF is worse, not broken.

    **For English that degradation is backed by a coded list**
    (``_FALLBACK_ENGLISH_STOPWORDS``); for every other language the result really is
    just the filler set. That asymmetry is deliberate and is explained at the constant:
    ``keyphrases.py`` splits *on* these words to find candidate boundaries, so for it an
    empty set is fatal rather than degrading, and it only ever runs over English text.
    """
    if language in _stopword_cache:
        return _stopword_cache[language]

    from app.utils.text_preprocessing import TRANSCRIPT_FILLER

    words: set[str] = set()
    corpus_name = _STOPWORD_LANG_MAP.get(language, "english")
    try:
        from nltk.corpus import stopwords as nltk_stopwords

        words = set(nltk_stopwords.words(corpus_name))
    # The three ways "the corpus is not here" actually presents, and nothing wider: a
    # blanket `except Exception` would swallow a defect in our own code as a quietly
    # reduced stopword set, which is precisely how the RAKE breakage above stayed
    # invisible. LookupError is NLTK's "resource not downloaded"; OSError covers a
    # corrupt or unreadable corpus zip; ImportError covers nltk being absent entirely.
    except (LookupError, OSError, ImportError) as exc:
        logger.debug("NLTK stopwords unavailable for %r (%s); continuing without", language, exc)

    if corpus_name == "english":
        # `_FALLBACK_ENGLISH_STOPWORDS` is a no-op when the corpus loaded (every word in
        # it is in NLTK's own list) and is what keeps RAKE working when it did not.
        # TRANSCRIPT_FILLER is disfluencies only — "um", "uh" — so it supplies no
        # sentence-structure boundaries and cannot substitute for real stopwords.
        words |= set(TRANSCRIPT_FILLER) | _FALLBACK_ENGLISH_STOPWORDS

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
