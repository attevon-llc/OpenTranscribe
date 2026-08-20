"""Deterministic document-language detection — stopword overlap, no model load.

Nothing here calls an LLM or loads a model, the same #403 **D6** reasoning the rest of
the no-LLM summary tier follows: this runs on the CPU document-parsing worker, so it
reuses the same NLTK stopword corpus ``ingest_artifacts.textrank.stopwords_for`` already
loads for the digest/keyphrase tiers rather than adding a new third-party dependency
(``langdetect``/``fasttext``/…) that would need its own pin, model download and
requirements-file entry.

**Distinct from ``ParseOptions.language``.** That field is an OCR *hint* the caller
supplies (``docling_serve.py``'s ``ocr_lang``) and most documents never set it, so
``ParsedDocument.language`` is ``None`` for nearly everything today — this module is
what actually looks at the parsed text.

**Declines rather than guesses.** The caller must never coerce an undetected result to
``"en"`` — that exact bug was just fixed in ``services/documents/chunking.py``'s
``_split_long_block``: a hardcoded ``"en"`` silently defeats
``chunking_service.split_into_sentences``'s own script/terminator guard (issue #448) for
every non-Latin document. Returning ``None`` here and passing it straight through is what
lets that guard's real no-language path fire instead of being pre-empted upstream.
"""

from __future__ import annotations

import re

#: Candidate languages — exactly the set ``ingest_artifacts.textrank.stopwords_for`` has
#: a real NLTK stopword list for (its ``_STOPWORD_LANG_MAP``, private to that module and
#: therefore not imported directly; kept in sync by
#: ``tests/unit/test_document_language_detection.py``).
CANDIDATE_LANGUAGES: tuple[str, ...] = (
    "ar",
    "da",
    "nl",
    "en",
    "fi",
    "fr",
    "de",
    "hu",
    "it",
    "no",
    "pt",
    "ro",
    "ru",
    "es",
    "sv",
    "el",
    "tr",
)

#: Word tokens only — digits and punctuation carry no language signal for this heuristic.
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

#: Below this many tokens there is not enough signal to trust a ratio: a three-word title
#: matching one stopword by chance would "detect" a language from noise.
MIN_TOKENS = 40

#: Below this stopword ratio the text is not confidently identified as ANY candidate —
#: could be code, a table, a language with no stopword list here, or simply short of
#: signal. This is the threshold that produces the "undetectable" outcome.
MIN_STOPWORD_RATIO = 0.03

#: Tokens sampled, purely for cost — a 500-page document does not need every page.
SAMPLE_TOKENS = 4000


def detect_document_language(text: str) -> str | None:
    """Best-effort ISO 639-1 code for *text*, or ``None`` when it cannot be told.

    Args:
        text: The document's canonical text (or a representative slice of it — the
            first :data:`SAMPLE_TOKENS` word tokens are used).

    Returns:
        A code from :data:`CANDIDATE_LANGUAGES`, or ``None``. Never a guess dressed up
        as a detection, and never a coerced default.
    """
    from app.services.ingest_artifacts.textrank import stopwords_for

    tokens = [t.lower() for t in _WORD_RE.findall(text or "")][:SAMPLE_TOKENS]
    if len(tokens) < MIN_TOKENS:
        return None

    best_lang: str | None = None
    best_count = -1
    # CANDIDATE_LANGUAGES is a fixed, ordered tuple, so ties break on the earlier
    # candidate deterministically — no dependence on any per-process set-ordering
    # variance in how `stopwords_for` built its result.
    for lang in CANDIDATE_LANGUAGES:
        stops = stopwords_for(lang)
        count = sum(1 for token in tokens if token in stops)
        if count > best_count:
            best_count = count
            best_lang = lang

    if best_lang is None or best_count <= 0:
        return None
    ratio = best_count / len(tokens)
    if ratio < MIN_STOPWORD_RATIO:
        return None
    return best_lang
