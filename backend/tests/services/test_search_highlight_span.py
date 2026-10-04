"""Highlight spans must cover exactly the matched words, never the word after them.

Root cause (reproduced on OpenSearch 3.4): ``content`` is analyzed with a shingle filter
over stopword-stripped text, so its index holds filler shingles such as ``remot control _``
(the ``_`` standing in for the removed stopword "is"). A fuzzy AND ``multi_match`` on that
field lets the analyzed query shingle ``remot control`` fuzzily match ``remot control _``,
and the highlighter then marks that shingle's whole offset range, running past the phrase:
``<mark>remote control is. Then </mark>``. Fuzzy matching must therefore never target the
shingled field.
"""

from app.services.search.hybrid_search_service import HybridSearchService

QUERY = "remote control"
HYBRID_FIELDS = ["content^3", "content.exact^2", "title^2", "speaker^3"]


def _fuzzy_clauses(text_query: dict) -> list[dict]:
    return [
        c["multi_match"] for c in text_query["bool"]["should"] if c["multi_match"].get("fuzziness")
    ]


def _build(query: str, fields: list[str]) -> dict:
    return HybridSearchService.__new__(HybridSearchService)._build_text_query(query, fields)


def test_multi_word_fuzzy_clause_skips_shingled_content_field():
    clauses = _fuzzy_clauses(_build(QUERY, HYBRID_FIELDS))
    assert clauses, "multi-word queries keep their typo-tolerant clause"
    for clause in clauses:
        bare = [f for f in clause["fields"] if f.split("^")[0] == "content"]
        assert bare == [], f"fuzzy clause targets the shingled field: {clause['fields']}"
        assert "content.exact^2" in clause["fields"]


def test_fuzzy_clause_keeps_fields_when_no_unshingled_sibling():
    clauses = _fuzzy_clauses(_build(QUERY, ["content"]))
    assert clauses[0]["fields"] == ["content"]


def test_non_fuzzy_clauses_keep_stemmed_content_field():
    text_query = _build(QUERY, HYBRID_FIELDS)
    plain = [
        c["multi_match"]
        for c in text_query["bool"]["should"]
        if not c["multi_match"].get("fuzziness")
    ]
    assert plain
    for clause in plain:
        assert "content^3" in clause["fields"]
