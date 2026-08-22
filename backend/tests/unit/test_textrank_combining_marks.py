"""``ingest_artifacts/textrank.py``'s tokenizer must not split at Unicode combining
marks (lane C0 task 8).

``_TOKEN_RE`` used to be ``[^\\W\\d_]+(?:'[^\\W\\d_]+)?|\\d+(?:[.,]\\d+)*`` — Python's word
class minus digits and underscore. A combining mark (Unicode general category ``Mn``
nonspacing, or ``Mc`` spacing-combining) is neither a letter nor a digit under
Python's own classification, so it is NOT a member of that class. Thai vowel/tone
signs and Devanagari matras/virama/anusvara are exactly these marks, always attached
to a preceding consonant — so a base consonant followed by its own combining mark
tokenized as two (or more) separate one-character matches, corrupting both the
extractive digest and the keyphrase extraction for every language written this way,
silently: no exception, just fragments scored as if they were independent words.

Every assertion here is a token COUNT plus exact CONTENT check, not merely "did not
raise" — a regex that silently drops the mark entirely, or that merges unrelated
characters, would also run to completion without an exception.
"""

from __future__ import annotations

from app.services.ingest_artifacts.textrank import _TOKEN_RE
from app.services.ingest_artifacts.textrank import tokenize

#: Real Thai words, each containing at least one combining mark (verified via
#: ``unicodedata.category``): กำลัง (currently/progressive marker, contains Mn ั),
#: หิว (hungry, contains Mn ิ).
THAI_WORD_WITH_MARK = "กำลัง"
THAI_HUNGRY = "หิว"

#: Real Devanagari words, each containing at least one combining mark: हिंदी (Hindi —
#: Mc ि, Mn ं), नमस्ते (a greeting — Mn ्, Mn े).
DEVANAGARI_HINDI = "हिंदी"
DEVANAGARI_NAMASTE = "नमस्ते"


def test_an_isolated_thai_word_with_a_combining_mark_is_one_token():
    """The narrowest possible regression case: no sentence structure at all, just
    one grapheme cluster. The pre-fix regex split this into fragments (measured:
    ``['กำล', 'ง']`` for this exact word) because the mark ``ั`` matched neither
    ``[^\\W\\d_]`` (a letter) nor was it merged with its neighbours."""
    assert _TOKEN_RE.findall(THAI_WORD_WITH_MARK) == [THAI_WORD_WITH_MARK]
    assert _TOKEN_RE.findall(THAI_HUNGRY) == [THAI_HUNGRY]


def test_an_isolated_devanagari_word_with_combining_marks_is_one_token():
    assert _TOKEN_RE.findall(DEVANAGARI_HINDI) == [DEVANAGARI_HINDI]
    assert _TOKEN_RE.findall(DEVANAGARI_NAMASTE) == [DEVANAGARI_NAMASTE]


def test_a_thai_phrase_tokenizes_to_exactly_its_three_space_separated_words():
    """Thai does not require Devanagari's spaces to make this module's point — this
    phrase uses them (a common convention at clause boundaries) precisely so the
    expected token boundary is unambiguous: at the spaces, never inside a word."""
    phrase = "กำลัง เรียน ภาษาไทย"  # "currently studying [the] Thai language"
    tokens = tokenize(phrase, language="th", stem=False)
    assert len(tokens) == 3, f"expected 3 tokens, got {len(tokens)}: {tokens}"
    assert tokens == ["กำลัง", "เรียน", "ภาษาไทย"]


def test_a_devanagari_sentence_tokenizes_to_exactly_its_five_words():
    sentence = "मैं हिंदी सीख रहा हूँ"  # "I am learning Hindi"
    tokens = tokenize(sentence, language="hi", stem=False)
    assert len(tokens) == 5, f"expected 5 tokens, got {len(tokens)}: {tokens}"
    assert tokens == ["मैं", "हिंदी", "सीख", "रहा", "हूँ"]


def test_english_tokenization_is_unaffected_by_the_mark_aware_class():
    """The fix widens the token class; it must not widen it for Latin text, where
    Python's plain word class was already correct and combining marks essentially
    never appear in ordinary English prose."""
    tokens = tokenize("The quick brown fox jumps over lazy dogs.", language="en", stem=False)
    assert tokens == ["quick", "brown", "fox", "jumps", "lazy", "dogs"]
