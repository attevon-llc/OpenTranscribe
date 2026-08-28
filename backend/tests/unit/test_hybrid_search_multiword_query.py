"""``HybridSearchService._build_text_query``'s multi-word gate (issue #606 follow-up).

#606 fixed semantic search missing an obvious gold file for compound-concept queries
by removing the OR-fuzzy clause from multi-word queries: OpenSearch's default OR
operator on a fuzzy ``multi_match`` matches if ANY ONE term fuzzily matches ANY token
in the field, so a two-word query where only one word happens to have a same-length,
edit-distance-2 near neighbour elsewhere in the corpus scores a full "keyword match"
for the whole phrase, even when the other word has zero support in that document.

A live adversarial re-audit of that fix found two further defects in the same
function, both reproduced live against a real OpenSearch index before being fixed
here (see the PR/commit description for the measured hit counts):

* **Finding 1 (regression #606 itself reintroduced)**: the multi-word decision was
  computed from a LENGTH-FILTERED word list (``len(w) >= 2``), so a genuinely
  multi-word query whose short token gets filtered out — ``"x exploration"`` — was
  scored as single-word and got the OR-fuzzy clause back, reopening the exact
  false-positive class #606 closed.
* **Finding 2 (pre-existing, adjacent)**: removing the OR-fuzzy clause for multi-word
  queries silently deleted ALL typo tolerance for them (the docstring still claimed
  "Fuzzy match: Handles typos" for every query). A second, ADDITIVE clause with
  ``operator: "and"`` restores it without reopening #606: every term must fuzzily
  match *something* in the same field, so a single lucky near-neighbour can no
  longer carry the whole query the way plain OR-fuzzy could.

These are pure query-body-construction tests — no OpenSearch, no DB. They assert on
the SHAPE of the constructed clauses, which is what the live reproduction actually
measured downstream (a false positive or a missing typo match is a direct
consequence of which clauses are present).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.search.hybrid_search_service import HybridSearchService

pytestmark = pytest.mark.unit

_FIELDS = ["content^3", "content.exact^2", "title^2", "speaker^3"]


def _build(query: str) -> dict[str, Any]:
    return HybridSearchService()._build_text_query(query, _FIELDS)


def _should_clauses(body: dict[str, Any]) -> list[dict[str, Any]]:
    should: list[dict[str, Any]] = body["bool"]["should"]
    return should


def _multi_matches(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [c["multi_match"] for c in _should_clauses(body)]


def _or_fuzzy_clauses(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The #606 false-positive shape: fuzziness with no ``operator: "and"`` guard."""
    return [
        mm
        for mm in _multi_matches(body)
        if mm.get("fuzziness") == "AUTO" and mm.get("operator") != "and"
    ]


def _and_fuzzy_clauses(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The finding-2 typo-tolerance shape: fuzziness gated behind operator "and"."""
    return [
        mm
        for mm in _multi_matches(body)
        if mm.get("fuzziness") == "AUTO" and mm.get("operator") == "and"
    ]


class TestSingleWordUnaffected:
    """The single-word path is untouched by either finding's fix."""

    def test_single_word_query_gets_the_or_fuzzy_clause(self):
        body = _build("exploration")
        assert len(_or_fuzzy_clauses(body)) == 1

    def test_single_word_query_gets_no_and_fuzzy_clause(self):
        body = _build("exploration")
        assert _and_fuzzy_clauses(body) == []

    def test_single_word_query_gets_no_cross_field_or_phrase_clause(self):
        body = _build("exploration")
        types = {mm["type"] for mm in _multi_matches(body)}
        assert "cross_fields" not in types
        assert "phrase" not in types


class TestMultiWordWordCountIsRaw:
    """Finding 1: the multi-word decision must use the RAW split, not a
    length-filtered one — a query with a short token is still multi-word.
    """

    @pytest.mark.parametrize(
        "query",
        [
            "x exploration",  # the exact live-reproduced case
            "a b",  # both tokens filtered by the old `len(w) >= 2` guard
            "i explore",  # one filtered single-char token, one real word
        ],
    )
    def test_a_short_leading_token_still_counts_as_multi_word(self, query):
        body = _build(query)
        assert _or_fuzzy_clauses(body) == [], (
            f"query {query!r} got the single-word OR-fuzzy clause — "
            "a short token must not make this look single-word"
        )

    def test_x_exploration_gets_cross_field_and_phrase_clauses(self):
        """Positive control: confirms the multi-word branch actually ran,
        rather than the OR-fuzzy clause merely being absent by coincidence.
        """
        body = _build("x exploration")
        types = {mm["type"] for mm in _multi_matches(body)}
        assert "cross_fields" in types
        assert "phrase" in types

    def test_two_real_words_are_still_multi_word(self):
        """Control: an unambiguous multi-word query behaves the same way,
        confirming the parametrized cases above are testing the short-token
        edge rather than a change to ordinary multi-word handling.
        """
        body = _build("space exploration")
        assert _or_fuzzy_clauses(body) == []
        types = {mm["type"] for mm in _multi_matches(body)}
        assert "cross_fields" in types
        assert "phrase" in types


class TestMultiWordAndFuzzyIsAdditive:
    """Finding 2: multi-word typo tolerance is restored via a SECOND clause,
    added alongside cross-field/phrase, never replacing them.
    """

    def test_multi_word_query_gets_exactly_one_and_fuzzy_clause(self):
        body = _build("space exploration")
        and_clauses = _and_fuzzy_clauses(body)
        assert len(and_clauses) == 1
        assert and_clauses[0]["type"] == "best_fields"
        assert and_clauses[0]["prefix_length"] == 1

    def test_the_and_fuzzy_clause_is_additive_not_a_replacement(self):
        """cross_fields and phrase must still be present beside it — the
        auditor measured that replacing the OR clause with an AND-fuzzy one
        (rather than adding it) drops a legitimate non-typo multi-word query
        to 0 results on the live index.
        """
        body = _build("space exploration")
        types = [mm["type"] for mm in _multi_matches(body)]
        assert types.count("best_fields") == 2  # exact-boost + and-fuzzy
        assert "cross_fields" in types
        assert "phrase" in types

    def test_multi_word_still_gets_no_or_fuzzy_clause(self):
        """The finding-2 fix must not reintroduce #606's OR-fuzzy clause."""
        body = _build("space exploration")
        assert _or_fuzzy_clauses(body) == []


class TestQuotedPhraseUnaffected:
    def test_a_quoted_phrase_gets_only_a_phrase_clause(self):
        body = _build('"space exploration"')
        multi_matches = _multi_matches(body)
        assert len(multi_matches) == 1
        assert multi_matches[0]["type"] == "phrase"
        assert "fuzziness" not in multi_matches[0]
