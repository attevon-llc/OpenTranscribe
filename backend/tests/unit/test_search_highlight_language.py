"""Search highlighting must stem in the document's language, not always English (#453).

``_get_word_stem`` declared ``language: str = "english"`` and **all four call sites passed
nothing**, so every snippet on a multilingual index was stemmed by the English Snowball
rules. That is not a near-miss: English stemming of Spanish is a no-op
(``"hablando"`` -> ``"hablando"``, ``"presupuesto"`` -> ``"presupuesto"``), so the stem leg
of the highlighter — the leg that exists to match inflected forms — silently contributed
nothing on every non-English file.

Two properties are pinned here, and the second is the one that a per-query fix would miss:

1. The stemmer is chosen from the DOCUMENT's language.
2. A result page can mix languages, so the choice is **per hit**, not per page. A single
   context built once per page is correct-looking and wrong for every hit that is not in the
   page's first language.

⚠️ **A language with no Snowball stemmer must not fall back to English.** Chinese, Japanese
and Korean are not stemmed languages; running English suffix-stripping over them produces
confident nonsense, and the resulting token matches nothing — strictly worse than leaving the
word alone. ``snowball_language_for`` returns ``None`` for those, and ``None`` means *do not
stem*.

Scope: highlighting only. This changes which words get a ``<mark class="semantic">``; it does
not touch the index, the analyzer, or ranking.
"""

from __future__ import annotations

import pytest

from app.services.search.hybrid_search_service import QueryHighlightContext
from app.services.search.hybrid_search_service import _get_word_stem
from app.services.search.hybrid_search_service import snowball_language_for


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("es", "spanish"),
        ("de", "german"),
        ("ru", "russian"),
        ("pt-BR", "portuguese"),
        ("pt_PT", "portuguese"),
        ("EN", "english"),
        ("nb", "norwegian"),
    ],
)
def test_iso_codes_resolve_to_snowball_names(code: str, expected: str) -> None:
    """The two vocabularies never met: documents carry ISO, Snowball is keyed by name."""
    assert snowball_language_for(code) == expected


@pytest.mark.parametrize("code", ["zh", "ja", "ko", "th", "hi", "", None])
def test_an_unstemmed_language_resolves_to_none_not_english(code: str | None) -> None:
    """None means DO NOT STEM. English suffix-stripping on CJK is worse than nothing."""
    assert snowball_language_for(code) is None, (
        f"{code!r} resolved to a stemmer; falling back to English produces a token that "
        "matches nothing, which is a silent loss rather than a visible error"
    )


def test_spanish_words_are_stemmed_in_spanish() -> None:
    """The defect, in one assertion: English stemming of Spanish is a no-op.

    ``hablando`` and ``presupuesto`` are unchanged by the English rules, so the stem leg
    of the highlighter contributed nothing at all on a Spanish transcript.
    """
    ctx = QueryHighlightContext.from_query("hablando presupuesto", "es")

    assert ctx.stem_language == "spanish"
    assert ctx.query_stems == ["habl", "presupuest"], (
        f"expected Spanish stems, got {ctx.query_stems} — these are the English results, "
        "which for Spanish input are the input"
    )


def test_english_pages_are_unchanged() -> None:
    """The control: the existing single-language behaviour must not move."""
    ctx = QueryHighlightContext.from_query("running meetings", "en")

    assert ctx.stem_language == "english"
    assert ctx.query_stems == [_get_word_stem(w, "english") for w in ("running", "meetings")]


def test_an_unstemmed_language_leaves_the_words_as_their_own_stems() -> None:
    """Not stemming must still produce a usable stem list, or the leg raises or drops out."""
    ctx = QueryHighlightContext.from_query("会議 予算", "ja")

    assert ctx.stem_language is None
    assert ctx.query_stems == ctx.query_words


@pytest.mark.parametrize("english_first", [False, True], ids=["es-first", "en-first"])
def test_a_mixed_language_page_gets_one_context_per_language(english_first: bool) -> None:
    """The per-page bug: one context stems every hit as whichever language came first.

    ⚠️ **The fixture is chosen so the STEM leg is the only leg that can fire.** The first
    draft of this test used a snippet containing the query word verbatim — which the
    exact-match leg highlights with no stemmer involved at all, so it passed against the
    unfixed code and proved nothing. ``hablando`` (query) vs ``hablamos`` (snippet) share a
    Spanish stem (``habl``) but not an English one (``hablando`` vs ``hablamo``), and the
    prefix leg cannot bridge them either (the prefix is ``hablan``).

    Both orderings are asserted: a single page-wide context passes whichever order happens
    to put its language first, so one order alone is a coin flip rather than a test.
    """
    from types import SimpleNamespace

    from app.services.search.hybrid_search_service import HybridSearchService

    def _hit(language: str, snippet: str):
        occ = SimpleNamespace(snippet=snippet, has_keyword_match=False)
        return SimpleNamespace(language=language, occurrences=[occ])

    spanish = _hit("es", "ayer hablamos del presupuesto")
    english = _hit("en", "we discussed the budget")
    page = [english, spanish] if english_first else [spanish, english]

    HybridSearchService._apply_semantic_highlights(
        HybridSearchService.__new__(HybridSearchService), page, "hablando"
    )

    assert "<mark" in spanish.occurrences[0].snippet, (
        "the Spanish hit was not highlighted: 'hablando' stems to itself under the English "
        f"rules, so it never reaches 'hablamos' — got {spanish.occurrences[0].snippet!r}"
    )
    # Control: the query has no bearing on the English hit under either stemmer, so a mark
    # here would mean a Spanish stem was applied to an English document.
    assert "<mark" not in english.occurrences[0].snippet, (
        f"the English hit was highlighted by a Spanish stem: {english.occurrences[0].snippet!r}"
    )
