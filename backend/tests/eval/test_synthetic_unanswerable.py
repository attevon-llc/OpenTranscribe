"""Tests for ``synthetic.unanswerable`` — absent-entity probes and false_answer_rate (#463).

No LLM, no corpus, no stack: pure deterministic generation and a pure text heuristic.
"""

from __future__ import annotations

import pytest

from tests.eval.synthetic.unanswerable import ABSENT_ENTITIES
from tests.eval.synthetic.unanswerable import DEFAULT_PROBE_COUNT
from tests.eval.synthetic.unanswerable import false_answer_rate
from tests.eval.synthetic.unanswerable import is_false_answer
from tests.eval.synthetic.unanswerable import plant_unanswerable_probes

FILES = ["file-a", "file-b", "file-c"]


class TestPlantUnanswerableProbes:
    def test_default_count_is_thirty(self) -> None:
        assert DEFAULT_PROBE_COUNT == 30
        probes = plant_unanswerable_probes(FILES)
        assert len(probes) == 30

    def test_custom_count_is_respected(self) -> None:
        assert len(plant_unanswerable_probes(FILES, count=5)) == 5

    def test_empty_file_list_plants_nothing(self) -> None:
        assert plant_unanswerable_probes([], count=10) == []

    def test_same_inputs_plant_byte_identical_probes(self) -> None:
        first = plant_unanswerable_probes(FILES, seed=3)
        second = plant_unanswerable_probes(FILES, seed=3)
        assert first == second

    def test_different_seed_produces_different_file_assignment(self) -> None:
        a = plant_unanswerable_probes(FILES, seed=1)
        b = plant_unanswerable_probes(FILES, seed=2)
        assert [p.file_uuid for p in a] != [p.file_uuid for p in b]

    def test_every_probe_id_is_unique(self) -> None:
        probes = plant_unanswerable_probes(FILES, count=30)
        assert len({p.probe_id for p in probes}) == 30

    def test_every_probe_references_an_absent_entity(self) -> None:
        probes = plant_unanswerable_probes(FILES, count=30)
        assert len(probes) == 30, "plant_unanswerable_probes produced no probes to check"
        for probe in probes:
            assert probe.entity in ABSENT_ENTITIES
            assert probe.entity in probe.text

    def test_every_probe_is_scoped_to_one_of_the_given_files(self) -> None:
        probes = plant_unanswerable_probes(FILES, count=10)
        assert all(p.file_uuid in FILES for p in probes)

    def test_more_probes_than_entities_cycles_the_entity_pool(self) -> None:
        """30 probes over 20 entities means the pool repeats -- this is fine, the
        probe id is still unique per index, and the point (absence) doesn't
        change by asking about the same absent entity twice with different
        phrasing."""
        probes = plant_unanswerable_probes(FILES, count=30)
        entities_used = {p.entity for p in probes}
        assert len(entities_used) <= len(ABSENT_ENTITIES)
        assert len(entities_used) > 1  # not degenerately collapsed to one


class TestIsFalseAnswer:
    def test_a_confident_fabricated_answer_is_false(self) -> None:
        assert is_false_answer("Zorblatt Industries raised concerns about the timeline.") is True

    def test_none_is_not_a_false_answer(self) -> None:
        assert is_false_answer(None) is False

    def test_blank_string_is_not_a_false_answer(self) -> None:
        assert is_false_answer("   ") is False

    @pytest.mark.parametrize(
        "decline",
        [
            "Zorblatt Industries is not mentioned in this recording.",
            "I don't have any information about that.",
            "There is no record of that entity in the transcript.",
            "I couldn't find any mention of Zorblatt Industries.",
        ],
    )
    def test_various_genuine_declines_are_not_false_answers(self, decline: str) -> None:
        assert is_false_answer(decline) is False

    def test_marker_matching_is_case_insensitive(self) -> None:
        assert is_false_answer("NOT MENTIONED anywhere in the recording.") is False


class TestFalseAnswerRate:
    def test_all_fabricated_scores_1(self) -> None:
        probes = plant_unanswerable_probes(FILES, count=3)
        submitted = {p.probe_id: f"{p.entity} discussed the roadmap." for p in probes}
        assert false_answer_rate(probes, submitted) == 1.0

    def test_all_declined_scores_0(self) -> None:
        probes = plant_unanswerable_probes(FILES, count=3)
        submitted = {p.probe_id: "That entity is not mentioned in this recording." for p in probes}
        assert false_answer_rate(probes, submitted) == 0.0

    def test_mixed_gives_the_real_fraction(self) -> None:
        probes = plant_unanswerable_probes(FILES, count=4)
        submitted = {
            probes[0].probe_id: "Not mentioned.",
            probes[1].probe_id: f"{probes[1].entity} said the budget was fine.",
            probes[2].probe_id: "No record of that.",
            probes[3].probe_id: f"{probes[3].entity} attended in person.",
        }
        assert false_answer_rate(probes, submitted) == pytest.approx(0.5)

    def test_a_probe_missing_from_submitted_counts_as_a_decline_not_dropped(self) -> None:
        """The denominator is always len(probes) -- an absent submission is
        treated as None (a decline), never silently excluded."""
        probes = plant_unanswerable_probes(FILES, count=3)
        submitted = {probes[0].probe_id: f"{probes[0].entity} was very active."}
        # probes[1] and probes[2] are absent from `submitted` entirely.
        assert false_answer_rate(probes, submitted) == pytest.approx(1 / 3)

    def test_empty_probes_raises(self) -> None:
        with pytest.raises(ValueError, match="no probes"):
            false_answer_rate([], {})
