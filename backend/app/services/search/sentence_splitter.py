"""Sentence splitting and word counting shared by the chunker and the digest.

``split_into_sentences`` is public on purpose: ``ingest_artifacts/digest.py`` and
the transcript chunker must split identically. Owns the process-latched punkt
state (``_nltk_load_failed``, ``_nltk_tokenizers``) that issue #449 made permanent.
"""

import logging
import re
from typing import Any

from app.utils.nltk_offline import NLTK_CORPUS_UNAVAILABLE

logger = logging.getLogger(__name__)

# NLTK punkt language map: ISO 639-1 → NLTK punkt language name.
# punkt_tab ships tokenizers for these 18 languages.
_PUNKT_LANG_MAP: dict[str, str] = {
    "cs": "czech",
    "da": "danish",
    "nl": "dutch",
    "en": "english",
    "et": "estonian",
    "fi": "finnish",
    "fr": "french",
    "de": "german",
    "el": "greek",
    "it": "italian",
    "no": "norwegian",
    "pl": "polish",
    "pt": "portuguese",
    "ru": "russian",
    "sl": "slovene",
    "es": "spanish",
    "sv": "swedish",
    "tr": "turkish",
}

# Cache of loaded NLTK tokenizers keyed by language name
_nltk_tokenizers: dict[str, Any] = {}

#: Latched once per process: has an NLTK load already been attempted and failed?
#:
#: This replaces a 5-minute ``_nltk_unavailable_until`` retry cooldown, which made
#: chunk boundaries **time-dependent within a single re-index** (issue #449). punkt
#: and the regex fallback disagree on abbreviations — punkt keeps ``Dr.`` and
#: ``p.m.`` inside a sentence, the regex splits on them — and sentence boundaries
#: drive chunk boundaries. So after one failed load, files early in a pass were
#: chunked by the regex and files more than five minutes later by punkt: the same
#: corpus, chunked two different ways, in one run, with nothing recording which.
#:
#: That is issue #433's failure mode ("re-indexing one unchanged corpus produced
#: three different chunk counts") arriving by a second route after the ordering
#: bug behind it was fixed.
#:
#: Latching trades a retry for determinism, which is the right trade here: NLTK is
#: either installed in the image with its corpora or it is not, and that does not
#: change while the process runs. A worker that starts without punkt now chunks
#: the whole pass consistently rather than switching part-way through.
_nltk_load_failed: bool = False


def _get_nltk_tokenizer(language: str = "english"):
    """Load the NLTK punkt sentence tokenizer for the given language.

    Tokenizers are cached after first load. Falls back to English if the
    requested language model is not available, then to None if NLTK itself is
    unavailable — and that None is **latched for the life of the process**, so a
    re-index cannot switch splitters part-way through (issue #449).

    Args:
        language: NLTK punkt language name (e.g. "english", "german").

    Returns:
        The tokenizer on success, or None if NLTK/punkt is unavailable.
    """
    global _nltk_load_failed

    if _nltk_load_failed:
        return None

    if language in _nltk_tokenizers:
        return _nltk_tokenizers[language]

    try:
        import nltk.data

        tokenizer = _load_punkt_model(nltk.data, language)
        if tokenizer is None and language != "english":
            tokenizer = _load_punkt_model(nltk.data, "english")
        if tokenizer is not None:
            _nltk_tokenizers[language] = tokenizer
            logger.debug(f"Loaded NLTK punkt tokenizer for '{language}'")
            return tokenizer
    except Exception as e:
        logger.debug(f"NLTK punkt tokenizer not available, using regex fallback: {e}")

    # WARNING, not debug: this decides how every chunk in this process is split,
    # and a worker that silently differs from its peers produces an index whose
    # boundaries depend on which worker happened to handle each file. It is the
    # one line that makes a mixed index detectable after the fact.
    _nltk_load_failed = True
    logger.warning(
        "NLTK punkt unavailable; this process will use the REGEX sentence "
        "splitter for every transcript it chunks. The two disagree on "
        "abbreviations, so chunks produced here will not match those from a "
        "worker that has punkt. Install the punkt corpora to make the index "
        "uniform."
    )
    return None


def reset_sentence_splitter_state() -> None:
    """Clear the latched splitter decision and the tokenizer cache.

    For tests only. The latch is deliberately permanent in production — see
    ``_nltk_load_failed`` — so a suite that exercises both splitters needs an
    explicit way to undo it rather than waiting out a cooldown that no longer
    exists.
    """
    global _nltk_load_failed
    _nltk_load_failed = False
    _nltk_tokenizers.clear()


def _load_punkt_model(nltk_data_module: Any, language: str) -> Any:
    """Try loading punkt_tab first, fall back to punkt.

    Catches :data:`NLTK_CORPUS_UNAVAILABLE` rather than ``LookupError`` so the
    ``punkt`` fallback is actually reached when ``punkt_tab`` is present but
    *unreadable* — wrong ownership on the model cache, or nltk >=3.10 pathsec
    refusing a multiply-linked file (``PermissionError``). With the narrower
    guard the first failure escaped this function entirely and the second
    location was never tried, even when it would have worked.
    """
    try:
        return nltk_data_module.load(f"tokenizers/punkt_tab/{language}.pickle")
    except NLTK_CORPUS_UNAVAILABLE:
        pass
    try:
        return nltk_data_module.load(f"tokenizers/punkt/{language}.pickle")
    except NLTK_CORPUS_UNAVAILABLE:
        return None


# Scripts written WITHOUT spaces between words ("scriptio continua"): CJK
# ideographs, kana, and the Thai/Lao/Khmer/Myanmar families. Deliberately EXCLUDES
# Hangul — Korean is written with spaces, so counting it per-character would make
# Korean chunks several times smaller than every other language's.
_NO_SPACE_SCRIPT = (
    "぀-ヿ"  # hiragana + katakana
    "㐀-䶿"  # CJK ext A
    "一-鿿"  # CJK unified
    "豈-﫿"  # CJK compatibility
    "ｦ-ﾟ"  # halfwidth katakana
    "฀-๿"  # Thai
    "຀-໿"  # Lao
    "က-႟"  # Myanmar
    "ក-៿"  # Khmer
)
_NO_SPACE_CHAR_RE = re.compile(f"[{_NO_SPACE_SCRIPT}]")

#: Sentence terminators no punkt model recognises. Devanagari's danda is the one
#: that matters in practice: Hindi uses spaces, so it clears the scriptio-continua
#: check and would otherwise be handed to English punkt.
_FOREIGN_TERMINATOR_RE = re.compile("[。！？।॥…‥]")

#: One "word" for sizing purposes: either a single scriptio-continua character, or
#: a run of anything else that is not whitespace. Chunk budgets are expressed in
#: words, and ``str.split()`` reports a 10,000-character Chinese transcript as
#: **one word** — so every size check passed and the whole transcript became a
#: single chunk (issue #448). Counting each CJK character as a word is slightly
#: conservative (a character is nearer a syllable than a word), which errs toward
#: chunks that fit the embedding model's window rather than ones that overflow it.
_WORD_SPAN_RE = re.compile(f"[{_NO_SPACE_SCRIPT}]|[^\\s{_NO_SPACE_SCRIPT}]+")

# Regex fallback for sentence splitting when NLTK is unavailable, or when the
# script is one punkt has no model for.
#
# TWO alternatives, because the whitespace requirement differs. A Latin full stop
# needs the following space to avoid splitting "3.14" and "Dr. Chen"; the CJK and
# Devanagari terminators are never used inside a token, and the text that uses
# them has no spaces at all — so requiring ``\s+`` after them (as this pattern
# did) meant it never matched a single Chinese or Japanese sentence boundary.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[。！？…‥।॥])\s*")


def count_words(text: str) -> int:
    """Count words for chunk sizing, correctly for scripts written without spaces.

    Args:
        text: The text to measure.

    Returns:
        Number of word-equivalents. For Latin/Cyrillic/Hangul text this equals
        ``len(text.split())``; for CJK/Thai it counts characters, which
        ``str.split()`` cannot do because there is nothing to split on.
    """
    return len(_WORD_SPAN_RE.findall(text))


def _word_spans(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` offsets of each word-equivalent in *text*.

    Slicing the ORIGINAL string by these offsets preserves its spacing. The
    alternative — ``" ".join(text.split()[i:j])`` — inserts a space between every
    character of a Chinese chunk, corrupting both what the user reads and what
    the embedding model receives.
    """
    return [m.span() for m in _WORD_SPAN_RE.finditer(text)]


def _sentence_joiner(text: str) -> str:
    """The separator to rejoin sentences of *text* with.

    Empty for scriptio continua. The sentences were produced by splitting a
    string that had no spaces in it, so rejoining them with `" "` inserts
    separators the original never had — visible to the reader as
    ``产品。 达娜`` and fed to the embedding model as different text than was
    indexed elsewhere.
    """
    return "" if _NO_SPACE_CHAR_RE.search(text) else " "


def _punkt_can_read(text: str, language: str | None) -> bool:
    """Whether an NLTK punkt model is a sensible splitter for this text.

    ``_PUNKT_LANG_MAP.get(language, "english")`` used to send every unmapped
    language to the ENGLISH tokenizer, which loads fine, runs fine, and returns
    a Chinese transcript as **one sentence** — English punkt looks for ``.``,
    ``!``, ``?`` and Chinese uses ``。``, ``！``, ``？``. The regex fallback below
    already handled those terminators but was never reached, because punkt had
    not failed; it had merely been wrong.

    ``language=None`` (unknown — no caller should ever coerce this to ``"en"``
    just because English is the default; see this module's
    #448 fix) falls straight through to the text-based disqualifiers below, the
    same as any other unmapped code.
    """
    if language in _PUNKT_LANG_MAP:
        return True
    # Two independent disqualifiers, and the second is easy to miss: Devanagari
    # is written WITH spaces, so the scriptio-continua check alone passed it to
    # English punkt, which does not know the danda `।` ends a sentence and
    # returned a whole Hindi transcript as one sentence.
    return not (_NO_SPACE_CHAR_RE.search(text) or _FOREIGN_TERMINATOR_RE.search(text))


def split_into_sentences(text: str, language: str | None = "en") -> list[str]:
    """Split text into sentences using NLTK punkt with regex fallback.

    **Public, and it has to be.** ``services/ingest_artifacts/digest.py`` and
    the transcript chunker split text the same way,
    so the digest's sentence boundaries and the index's chunk boundaries stay the
    SAME boundaries — a second implementation would drift and the digest would cite
    spans the chunks do not contain. ``test_compose_sentence_splitter_mounts`` also
    derives which workers need the punkt mount by walking the import graph outward
    from this name, so privatising it silently empties that check.

    Args:
        text: Input text to split.
        language: ISO 639-1 language code (e.g. "en", "de", "fr"), or ``None``
            when the caller genuinely does not know — pass ``None``, not a
            coerced ``"en"``: :func:`_punkt_can_read` only applies its
            script/terminator disqualifiers to a language it does not
            recognise, so defaulting an unknown language to English silently
            defeats that guard (issue #448) instead of taking its no-language
            path.

    Returns:
        List of sentence strings. Returns [text] if splitting fails.
    """
    if not text or not text.strip():
        return []

    if _punkt_can_read(text, language):
        # `language or ""`: None (unknown) never matches a real map key, so this
        # still falls through to "english" — same outcome as before, just typed
        # correctly now that `_punkt_can_read` legitimately returns True for a
        # None language (the no-language path taken when the script/terminator
        # checks find nothing disqualifying).
        nltk_lang = _PUNKT_LANG_MAP.get(language or "", "english")
        tokenizer = _get_nltk_tokenizer(nltk_lang)
        if tokenizer is not None:
            try:
                return list(tokenizer.tokenize(text))
            except Exception as e:
                logger.debug(f"NLTK tokenizer failed for language '{language}': {e}")

    # Regex fallback: split on sentence-ending punctuation followed by uppercase
    sentences = _SENTENCE_SPLIT_RE.split(text)
    return [s for s in sentences if s.strip()]
