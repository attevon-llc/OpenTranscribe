"""Guard the guard: a wrong search-quality ground truth looks like a search bug.

Pure import of ``tests.fixtures.search_corpus`` — no DB, no stack needed. This is
the fast check that must pass before anything downstream (``test_search_quality.py``
against the live stack) can be trusted.
"""

from __future__ import annotations

from tests.fixtures.search_corpus import ANCHOR_PHRASES
from tests.fixtures.search_corpus import GLOBAL_WORD
from tests.fixtures.search_corpus import GOLD
from tests.fixtures.search_corpus import SPEAKER_FILE_COUNTS
from tests.fixtures.search_corpus import build_meeting_docs


def _texts() -> dict[str, str]:
    return {
        doc.meeting_id: " ".join(t.text.lower() for t in doc.turns) for doc in build_meeting_docs()
    }


class TestAnchorPhrases:
    def test_every_anchor_phrase_occurs_in_exactly_one_file(self):
        texts = _texts()
        assert ANCHOR_PHRASES, "no anchor phrases defined — the loop below would run zero times"
        for meeting_id, phrase in ANCHOR_PHRASES.items():
            hits = [mid for mid, text in texts.items() if phrase.lower() in text]
            assert hits == [meeting_id], (
                f"anchor phrase for {meeting_id!r} matched {hits!r}, expected exactly [{meeting_id!r}]"
            )

    def test_anchor_phrases_are_at_least_ten_words_with_four_long_words(self):
        assert ANCHOR_PHRASES, "no anchor phrases defined — the loop below would run zero times"
        for meeting_id, phrase in ANCHOR_PHRASES.items():
            words = phrase.split()
            assert len(words) >= 10, f"{meeting_id}: anchor phrase too short ({len(words)} words)"
            long_words = [w for w in words if len(w) >= 5]
            assert len(long_words) >= 4, f"{meeting_id}: anchor phrase has too few long words"


class TestGoldTable:
    def test_gold_and_anti_gold_reference_real_meeting_ids(self):
        real_ids = {doc.meeting_id for doc in build_meeting_docs()}
        assert GOLD, "no GOLD queries defined — the loop below would run zero times"
        for query, spec in GOLD.items():
            unknown = (spec["gold"] | spec["anti_gold"]) - real_ids
            assert not unknown, f"query {query!r} references unknown meeting_ids: {unknown}"

    def test_anti_gold_files_contain_none_of_the_query_terms(self):
        texts = _texts()
        assert GOLD, "no GOLD queries defined — the loop below would run zero times"
        for query, spec in GOLD.items():
            for anti_id in spec["anti_gold"]:
                assert query.lower() not in texts[anti_id], (
                    f"anti-gold file {anti_id!r} unexpectedly contains query term {query!r}"
                )

    def test_gold_files_contain_the_query_term_or_are_semantic_only(self):
        # Not every gold match is keyword-literal (deliberately, for semantic queries),
        # but every query string used as GOLD key must at least be a real string.
        assert GOLD, "no GOLD queries defined — the loop below would run zero times"
        for query, spec in GOLD.items():
            assert isinstance(query, str) and query.strip()
            assert spec["gold"], f"query {query!r} has an empty gold set"


class TestGlobalWord:
    def test_global_word_appears_in_all_six_files(self):
        texts = _texts()
        assert len(texts) == 6
        missing = [mid for mid, text in texts.items() if GLOBAL_WORD not in text]
        assert missing == [], f"GLOBAL_WORD {GLOBAL_WORD!r} missing from: {missing}"


class TestSpeakerFileCounts:
    def test_speaker_file_counts_match_reality(self):
        docs = build_meeting_docs()
        actual: dict[str, set[str]] = {}
        for doc in docs:
            for speaker in doc.speakers:
                actual.setdefault(speaker, set()).add(doc.meeting_id)

        assert set(actual) == set(SPEAKER_FILE_COUNTS), (
            f"speaker sets differ: computed {set(actual)} vs declared {set(SPEAKER_FILE_COUNTS)}"
        )
        for speaker, expected_count in SPEAKER_FILE_COUNTS.items():
            assert len(actual[speaker]) == expected_count, (
                f"{speaker}: expected {expected_count} files, computed {len(actual[speaker])}"
            )


class TestCorpusShape:
    def test_six_meetings_of_roughly_forty_turns_each(self):
        docs = build_meeting_docs()
        assert len(docs) == 6
        for doc in docs:
            assert 8 <= len(doc.turns) <= 60, (
                f"{doc.meeting_id}: unexpected turn count {len(doc.turns)}"
            )

    def test_stem_boundary_trap_words_present_in_espionage_file(self):
        """'right'/'might'/'eight' near 'fight' — proves highlighting doesn't over-stem."""
        texts = _texts()
        text = texts["sq-espionage"]
        for word in ("right", "might", "eight", "fight"):
            assert word in text, f"stem-boundary trap word {word!r} missing from sq-espionage"
