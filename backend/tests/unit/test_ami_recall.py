"""Guards for the AMI per-item content-recall metric.

Every "it scores a hit" case is paired with a case asserting it does NOT — a
recall metric that returns True generously is worse than no metric, because it
is used to argue that a change helped.
"""

from __future__ import annotations

from tests.eval.harness.ami_recall import parse_reference_items
from tests.eval.harness.ami_recall import score_answer

# A real AMI <decisions> reference shape: tagged, one discrete item per line.
_REFERENCE = """[IS1009a] Selling price will be twenty five Euro.
[IS1009a] Company aims to profit fifty million Euro.
[IS1009b] They will eliminate teletext.
[IS1009c] The remote control will have a locator button for finding it."""


def test_it_parses_the_recording_tag_off_each_item():
    pairs = parse_reference_items(_REFERENCE)
    assert len(pairs) == 4
    assert pairs[0][0] == "IS1009a"
    assert "twenty five Euro" in pairs[0][1]
    assert {p[0] for p in pairs} == {"IS1009a", "IS1009b", "IS1009c"}


def test_an_untagged_reference_still_yields_one_item_per_line():
    """QMSum single-file references are prose, not tagged lists. A caller must
    not have to know which corpus a reference came from.
    """
    pairs = parse_reference_items("First point here.\nSecond point here.")
    assert [p[0] for p in pairs] == ["", ""]
    assert len(pairs) == 2


def test_an_answer_carrying_the_items_recalls_them():
    answer = (
        "The team decided the selling price will be twenty five Euro and the company "
        "aims to profit fifty million Euro. They will eliminate teletext entirely."
    )
    score = score_answer(answer, _REFERENCE)
    assert score.total == 4
    assert score.recalled == 3, [(i.text, round(i.overlap, 2)) for i in score.items]
    assert score.recordings_covered() == {"IS1009a", "IS1009b"}


def test_an_answer_missing_the_items_does_not_recall_them():
    """The control that makes the test above mean something. An on-topic answer
    that contains none of the actual decisions must score ~0, or the metric is
    measuring topicality rather than recall.
    """
    answer = (
        "The group discussed the remote control's appearance at some length, "
        "including the case material and the shape of the buttons."
    )
    score = score_answer(answer, _REFERENCE)
    assert score.recalled == 0, [(i.text, round(i.overlap, 2)) for i in score.items]
    assert score.recall == 0.0


def test_the_missed_items_are_reported_not_just_a_number():
    """A score alone cannot be acted on; the SPECIFIC missed items are the
    finding. This is why there is no embedding-similarity version of this metric.
    """
    answer = "The selling price will be twenty five Euro."
    score = score_answer(answer, _REFERENCE)
    missed = {i.text for i in score.missed}
    assert any("teletext" in m for m in missed)
    assert any("locator" in m for m in missed)
    assert all(isinstance(i.overlap, float) for i in score.missed)


def test_a_near_miss_and_a_total_miss_are_distinguishable():
    """Collapsing both to False hides which one you have. A near miss says the
    material was nearly there; a total miss says it was absent.
    """
    score = score_answer("They will eliminate the teletext feature.", _REFERENCE)
    by_text = {i.text: i.overlap for i in score.items}
    teletext = next(v for k, v in by_text.items() if "teletext" in k)
    locator = next(v for k, v in by_text.items() if "locator" in k)
    assert teletext > locator


def test_too_short_items_are_skipped_and_counted_never_silently_dropped():
    """A recall of 1/1 over a reference whose other items were skipped is a
    wrong number presented as a right one.
    """
    score = score_answer("anything at all", "[A] Yes.\n[B] The team chose the blue casing.")
    assert score.skipped == 1
    assert score.total == 1


def test_an_empty_reference_scores_zero_rather_than_dividing_by_zero():
    score = score_answer("some answer", "")
    assert score.total == 0
    assert score.recall == 0.0
    assert score.missed == []
