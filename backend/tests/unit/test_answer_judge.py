"""Guards for the LLM-as-judge and its calibration maths.

The Kappa cases matter more than the parsing ones: a calibration number that is
silently wrong is worse than no calibration, because it is used to license
tuning decisions.
"""

from __future__ import annotations

import pytest

from tests.eval.harness.answer_judge import agreement_report
from tests.eval.harness.answer_judge import build_judge_prompt
from tests.eval.harness.answer_judge import cohens_kappa
from tests.eval.harness.answer_judge import interpret_kappa
from tests.eval.harness.answer_judge import parse_judgement

# ------------------------------------------------------------------ Kappa


def test_perfect_agreement_on_varied_labels_is_kappa_one():
    a = ["FULL", "PARTIAL", "NONE", "REFUSED"]
    assert cohens_kappa(a, list(a)) == pytest.approx(1.0)


def test_agreement_driven_by_a_majority_class_is_heavily_discounted():
    """The whole point of Kappa. These two annotators match on 8 of 10 items, but
    only because both default to the majority label; on every item where a real
    judgement was required they disagree. Raw agreement calls that 0.80; Kappa
    discounts the chance component and calls it 0.44.

    (Worked by hand: observed 0.80, expected 0.64, so (0.80-0.64)/(1-0.64)=0.444.
    It is NOT ~0 because the disagreement is SYSTEMATIC — FULL vs PARTIAL every
    time — rather than random.)
    """
    judge = ["NONE"] * 8 + ["FULL", "FULL"]
    human = ["NONE"] * 8 + ["PARTIAL", "PARTIAL"]
    assert cohens_kappa(judge, human) == pytest.approx(0.444, abs=0.01)


def test_true_chance_level_agreement_is_kappa_zero_or_below():
    """The complementary case: balanced marginals with agreement at or below what
    chance predicts must not score positive."""
    judge = ["NONE"] * 5 + ["FULL"] * 5
    human = ["NONE", "NONE", "FULL", "FULL", "FULL", "NONE", "NONE", "NONE", "FULL", "FULL"]
    assert cohens_kappa(judge, human) <= 0.0


def test_raw_agreement_overstates_on_a_skewed_distribution():
    """The specific trap this module exists to prevent: on skewed labels the raw
    percentage looks like agreement where Kappa shows there is none. If this ever
    stops holding, the warning in the module docstring is wrong.
    """
    judge = ["NONE"] * 8 + ["FULL", "FULL"]
    human = ["NONE"] * 8 + ["PARTIAL", "PARTIAL"]
    report = agreement_report(judge, human)
    assert report["raw_agreement"] == pytest.approx(0.8)
    assert report["cohens_kappa"] == pytest.approx(0.444, abs=0.01)
    # 36 points — squarely inside the 33-41 range the literature reports, which is
    # why the module refuses to present raw agreement on its own.
    assert report["overstatement"] > 0.3, "raw agreement must be shown to overstate"


def test_systematic_disagreement_is_negative():
    assert cohens_kappa(["FULL", "NONE"] * 5, ["NONE", "FULL"] * 5) < 0


def test_a_single_label_used_by_both_reports_one_rather_than_dividing_by_zero():
    """Expected agreement is 1.0 here, so the chance-corrected form is 0/0. It is
    reported as 1.0 and the caller is told the sample had no label variety.
    """
    assert cohens_kappa(["NONE"] * 6, ["NONE"] * 6) == 1.0


def test_misaligned_annotator_sequences_raise_rather_than_silently_truncate():
    """Silently zipping to the shorter list would compute a Kappa over a subset
    while reporting it as the whole — a wrong number that looks right.
    """
    with pytest.raises(ValueError, match="differ in length"):
        cohens_kappa(["FULL", "NONE"], ["FULL"])


@pytest.mark.parametrize(
    ("kappa", "must_contain"),
    [
        (0.05, "do NOT tune"),
        (0.35, "directional"),
        (0.5, "RANKING"),
        (0.7, "usable"),
        (0.9, "perfect"),
    ],
)
def test_interpretation_bands_say_what_each_level_licenses(kappa, must_contain):
    assert must_contain in interpret_kappa(kappa)


# ------------------------------------------------------------------ parsing


def test_it_parses_a_well_formed_judge_reply():
    j = parse_judgement('{"label": "PARTIAL", "covered": 2, "total": 7, "why": "only price"}')
    assert (j.label, j.covered, j.total, j.degraded) == ("PARTIAL", 2, 7, False)


def test_it_finds_the_json_even_with_surrounding_prose():
    j = parse_judgement('Sure!\n{"label": "FULL", "covered": 3, "total": 3, "why": "all"}\nDone.')
    assert j.label == "FULL" and j.degraded is False


def test_an_unparseable_reply_degrades_and_is_marked_degraded():
    """A Kappa computed partly over fallback labels measures the regex, not the
    judge, so those items must be identifiable and excludable.
    """
    j = parse_judgement("I think it's pretty good overall.", answer="Some real answer.")
    assert j.degraded is True
    assert j.label == "NONE"


def test_the_fallback_still_recognises_a_refusal():
    j = parse_judgement("garbage", answer="The excerpts do not contain that information.")
    assert j.label == "REFUSED" and j.degraded is True


def test_an_unknown_label_is_not_accepted_verbatim():
    """A judge inventing "GOOD" must not widen the label set — that would break
    every Kappa computed against the fixed human scale.
    """
    j = parse_judgement('{"label": "GOOD", "covered": 1, "total": 2, "why": "x"}', answer="x")
    assert j.label in ("NONE", "REFUSED") and j.degraded is True


# ------------------------------------------------------------------ prompting


def test_the_prompt_survives_braces_in_transcript_text():
    """Transcript-derived text routinely contains braces; `format` would raise or
    interpolate. Same defence as services/chat/prompting.py.
    """
    prompt = build_judge_prompt("q?", "ref {evil} text", "answer {0} {name}")
    assert "{evil}" in prompt and "{0}" in prompt


def test_the_prompt_never_names_the_arm_or_system():
    """A judge that can tell which arm produced an answer is not a judge. The
    prompt must carry only question/reference/answer.
    """
    prompt = build_judge_prompt("q?", "r", "a").lower()
    for leak in ("arm", "baseline", "control", "config", "run ", "variant", "candidate_pool"):
        assert leak not in prompt, f"judge prompt leaks the identifier {leak!r}"
