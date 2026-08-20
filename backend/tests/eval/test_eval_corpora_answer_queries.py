"""Tests for ``harness.corpora.load_qmsum_answer_queries`` (#463).

Exercised end-to-end against a fixture in the REAL QMSum on-disk shape — verified
against the real NAS data while ``harness/corpora.py`` was written (see that module's
docstring): ``specific_query_list[].answer`` and ``general_query_list[].answer`` are
both plain non-empty strings, and 0 of 4,728 real specific-query answers were missing.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.eval.harness.corpora import SUMMARIZE
from tests.eval.harness.corpora import InjectedCorpus
from tests.eval.harness.corpora import load_qmsum_answer_queries

FILE_UUID = "3f2a9c10-0000-0000-0000-000000000000"


def _write_meeting(root: Path, domain: str, meeting_id: str, payload: dict) -> None:
    directory = root / "data" / domain / "all"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{meeting_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _corpus(
    tmp_path: Path, meeting_id: str = "education_21", domain: str = "Committee"
) -> InjectedCorpus:
    return InjectedCorpus(
        key="qmsum",
        name="QMSum",
        version="test",
        license_tier="A",
        root=tmp_path,
        file_uuid_by_meeting={meeting_id: FILE_UUID},
        extra_by_meeting={meeting_id: {"domain": domain}},
    )


class TestSpecificQueryAnswers:
    def test_a_specific_query_with_an_answer_is_loaded(self, tmp_path: Path) -> None:
        payload = {
            "meeting_transcripts": [{"speaker": "Philip Blaker", "content": "text"}],
            "general_query_list": [],
            "specific_query_list": [
                {
                    "query": "What does Qualification Wales regulate?",
                    "answer": "Qualifications and their assessment.",
                    "relevant_text_span": [["0", "0"]],
                }
            ],
        }
        _write_meeting(tmp_path, "Committee", "education_21", payload)
        queries = load_qmsum_answer_queries(_corpus(tmp_path))

        assert len(queries) == 1
        query = queries[0]
        assert query.scored_on == "answer_text"
        assert query.gold_text == "Qualifications and their assessment."
        assert query.gold_answer is None  # answer_text uses gold_text, not gold_answer
        assert len(query.spans) == 1
        assert query.spans[0].file_uuid == FILE_UUID

    def test_a_specific_query_with_no_answer_is_skipped(self, tmp_path: Path) -> None:
        payload = {
            "meeting_transcripts": [{"speaker": "Alice", "content": "text"}],
            "general_query_list": [],
            "specific_query_list": [
                {"query": "What happened?", "answer": "  ", "relevant_text_span": [["0", "0"]]}
            ],
        }
        _write_meeting(tmp_path, "Committee", "education_21", payload)
        queries = load_qmsum_answer_queries(_corpus(tmp_path))
        assert queries == []

    def test_spans_are_kept_as_faithfulness_context(self, tmp_path: Path) -> None:
        payload = {
            "meeting_transcripts": [{"speaker": "Alice", "content": "text"}] * 5,
            "general_query_list": [],
            "specific_query_list": [
                {"query": "q", "answer": "a", "relevant_text_span": [["1", "3"]]}
            ],
        }
        _write_meeting(tmp_path, "Committee", "education_21", payload)
        queries = load_qmsum_answer_queries(_corpus(tmp_path))
        assert queries[0].spans[0].start_turn == 1
        assert queries[0].spans[0].end_turn == 3

    def test_query_class_follows_the_same_summarize_prefix_rule(self, tmp_path: Path) -> None:
        payload = {
            "meeting_transcripts": [{"speaker": "Alice", "content": "text"}],
            "general_query_list": [],
            "specific_query_list": [
                {
                    "query": "Summarize the budget discussion.",
                    "answer": "a",
                    "relevant_text_span": [["0", "0"]],
                }
            ],
        }
        _write_meeting(tmp_path, "Committee", "education_21", payload)
        queries = load_qmsum_answer_queries(_corpus(tmp_path))
        assert queries[0].query_class == SUMMARIZE


class TestGeneralQueryAnswers:
    def test_a_general_query_is_loaded_as_summarize_gold_file_scoped(self, tmp_path: Path) -> None:
        payload = {
            "meeting_transcripts": [
                {"speaker": "Alice", "content": "text"},
                {"speaker": "Bob", "content": "more text"},
            ],
            "general_query_list": [
                {"query": "Summarize the whole meeting.", "answer": "The meeting covered X and Y."}
            ],
            "specific_query_list": [],
        }
        _write_meeting(tmp_path, "Committee", "education_21", payload)
        queries = load_qmsum_answer_queries(_corpus(tmp_path))

        assert len(queries) == 1
        query = queries[0]
        assert query.query_class == SUMMARIZE
        assert query.scored_on == "answer_text"
        assert query.gold_text == "The meeting covered X and Y."
        # Gold-file-scoped: exactly one span, naming only this meeting's file --
        # a runner.py `--scope gold-files` consumer restricts retrieval to it.
        assert len(query.spans) == 1
        assert query.spans[0].file_uuid == FILE_UUID

    def test_a_general_query_with_no_answer_is_skipped(self, tmp_path: Path) -> None:
        payload = {
            "meeting_transcripts": [{"speaker": "Alice", "content": "text"}],
            "general_query_list": [{"query": "Summarize the whole meeting.", "answer": ""}],
            "specific_query_list": [],
        }
        _write_meeting(tmp_path, "Committee", "education_21", payload)
        queries = load_qmsum_answer_queries(_corpus(tmp_path))
        assert queries == []

    def test_both_sources_combine_for_one_meeting(self, tmp_path: Path) -> None:
        payload = {
            "meeting_transcripts": [{"speaker": "Alice", "content": "text"}],
            "general_query_list": [
                {"query": "Summarize the whole meeting.", "answer": "gold summary"}
            ],
            "specific_query_list": [
                {
                    "query": "What was discussed?",
                    "answer": "gold specific",
                    "relevant_text_span": [["0", "0"]],
                }
            ],
        }
        _write_meeting(tmp_path, "Committee", "education_21", payload)
        queries = load_qmsum_answer_queries(_corpus(tmp_path))
        assert len(queries) == 2
        gold_texts = {q.gold_text for q in queries}
        assert gold_texts == {"gold summary", "gold specific"}


class TestMissingMeetingFile:
    def test_a_missing_meeting_json_produces_no_queries_not_an_error(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path, meeting_id="never_written")
        assert load_qmsum_answer_queries(corpus) == []
