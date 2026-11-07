"""The judge layer with fake clients: blindness, shuffling, degraded replies, calibration."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

import pytest

from tests.eval.harness.summary_judge import Judged
from tests.eval.harness.summary_judge import calibration_report
from tests.eval.harness.summary_judge import export_calibration
from tests.eval.harness.summary_judge import judge_cell
from tests.eval.harness.summary_judge import judge_claims
from tests.eval.harness.summary_judge import judge_disclosure
from tests.eval.harness.summary_judge import judge_policy
from tests.eval.harness.summary_judge import judge_recall
from tests.eval.harness.summary_judge import sentence_id
from tests.eval.harness.summary_judge import shuffled
from tests.eval.harness.summary_judge import truncation_candidates

ITEMS = [("g1", "The price is twenty five euros"), ("g2", "The case is yellow rubber")]


class FakeJudge:
    """Records every prompt; replies from a script, or with a function of the prompt."""

    def __init__(self, *replies: str) -> None:
        self.prompts: list[str] = []
        self._replies = list(replies)

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]


class RoutedJudge(FakeJudge):
    """Answers each of the three question types with its own reply, whatever the call order."""

    def __init__(
        self, *, recall: str = "{}", claims: str = "{}", nothing: str = "{}", disclosure: str = "{}"
    ) -> None:
        super().__init__()
        self._by_kind = {
            "ITEMS:": recall,
            "WHOLE MEETING": nothing,
            "RECORDING or TRANSCRIPT": claims,
            "DISCLOSED:": disclosure,
        }

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(reply for marker, reply in self._by_kind.items() if marker in prompt)


def _conveyed_by_text(needle: str) -> Callable[[str], str]:
    """A judge that answers 'conveyed' for whichever number carries ``needle``."""
    """A judge that answers 'conveyed' for whichever number carries ``needle``."""

    def reply(prompt: str) -> str:
        lines = [ln for ln in prompt.split("SUMMARY:")[0].splitlines() if ln[:1].isdigit()]
        return json.dumps({"conveyed": [int(ln.split(".")[0]) for ln in lines if needle in ln]})

    return reply


class TestRecall:
    def test_maps_the_judge_numbers_back_to_item_ids_whatever_the_shuffle_did(self) -> None:
        for seed in range(6):
            result = judge_recall(_conveyed_by_text("yellow"), "A yellow case.", ITEMS, seed=seed)
            assert result.value == {"g2"}

    def test_items_are_shuffled_with_a_fixed_seed_and_the_prompt_is_stable(self) -> None:
        first, second = FakeJudge('{"conveyed": []}'), FakeJudge('{"conveyed": []}')
        judge_recall(first, "S", ITEMS, seed=3)
        judge_recall(second, "S", ITEMS, seed=3)
        assert first.prompts == second.prompts
        assert shuffled(list(range(20)), seed=1) != list(range(20))

    def test_the_prompt_carries_only_items_and_summary(self) -> None:
        fake = FakeJudge('{"conveyed": []}')
        judge_recall(fake, "SUMMARY-BODY", ITEMS)
        prompt = fake.prompts[0]
        assert "SUMMARY-BODY" in prompt
        assert "twenty five euros" in prompt
        assert "g1" not in prompt  # ids are bookkeeping; the judge sees numbers only

    @pytest.mark.parametrize(
        "reply",
        ["no json here", '{"conveyed": "all"}', '{"conveyed": [9]}', '{"conveyed": [true]}'],
    )
    def test_an_unusable_reply_is_degraded_not_scored(self, reply: str) -> None:
        result = judge_recall(FakeJudge(reply), "S", ITEMS)
        assert result.degraded
        assert result.value is None

    def test_no_items_means_no_call(self) -> None:
        fake = FakeJudge("{}")
        assert judge_recall(fake, "S", []).value == set()
        assert fake.prompts == []


class TestClaims:
    def test_returns_the_sentences_the_judge_confirmed(self) -> None:
        sentences = ["The recording ends abruptly.", "The team cut the budget."]

        def reply(prompt: str) -> str:
            lines = [ln for ln in prompt.splitlines() if ln[:1].isdigit()]
            hit = next(int(ln.split(".")[0]) for ln in lines if "abruptly" in ln)
            return json.dumps({"claims_incomplete": [hit]})

        assert judge_claims(reply, sentences).value == ["The recording ends abruptly."]

    def test_a_degraded_reply_is_flagged(self) -> None:
        assert judge_claims(FakeJudge("nope"), ["x"]) == Judged(None, degraded=True)


class TestDisclosure:
    def test_parses_both_labels(self) -> None:
        reply = '{"disclosed": true, "absence_claim": false}'
        assert judge_disclosure(FakeJudge(reply), "S").value == (True, False)

    def test_missing_or_non_boolean_labels_are_degraded(self) -> None:
        degraded = Judged(None, degraded=True)
        assert judge_disclosure(FakeJudge('{"disclosed": true}'), "S") == degraded
        assert (
            judge_disclosure(FakeJudge('{"disclosed": "yes", "absence_claim": 0}'), "S") == degraded
        )


class TestTruncationCandidates:
    TEXT = "One. Two. Three. Four. Five. The recording ends abruptly. Seven. Eight. Nine. Ten."

    def test_returns_every_hit_and_a_fixed_fifth_of_the_non_hits(self) -> None:
        hits, sample = truncation_candidates(self.TEXT, seed=5)
        assert hits == ["The recording ends abruptly."]
        assert len(sample) == 2  # 9 non-hits -> round(1.8)
        assert hits[0] not in sample
        assert truncation_candidates(self.TEXT, seed=5) == (hits, sample)

    def test_a_different_seed_samples_different_sentences(self) -> None:
        samples = {tuple(truncation_candidates(self.TEXT, seed=s)[1]) for s in range(8)}
        assert len(samples) > 1


class TestJudgeCell:
    def test_is_blind_by_construction(self) -> None:
        params = set(inspect.signature(judge_cell).parameters)
        assert not params & {"arm", "arm_name", "input_id", "run", "cell_id"}

    def test_a_confirmed_claim_fails_b_unless_a_slice_said_so(self) -> None:
        kw: dict[str, Any] = {
            "summary_text": "The recording ends abruptly. The team agreed.",
            "missed_items": [],
            "failure_input": False,
            "seed": 1,
        }

        # Sentences are shuffled before numbering, so confirm whichever number carries the claim.
        def confirm_abrupt(prompt: str) -> str:
            lines = [ln for ln in prompt.splitlines() if ln[:1].isdigit()]
            hit = next(int(ln.split(".")[0]) for ln in lines if "abruptly" in ln)
            return json.dumps({"claims_incomplete": [hit]})

        assert judge_cell(confirm_abrupt, slice_says_truncated=False, **kw)["b_fail"]
        assert not judge_cell(confirm_abrupt, slice_says_truncated=True, **kw)["b_fail"]

    def test_the_judge_clearing_a_prefilter_hit_leaves_b_passing(self) -> None:
        out = judge_cell(
            RoutedJudge(claims='{"claims_incomplete": []}'),
            summary_text="The recording ends abruptly.",
            missed_items=[],
            slice_says_truncated=False,
            failure_input=False,
            seed=1,
        )
        assert out["b_fail"] is False

    def test_a_claim_on_a_sentence_the_regex_passed_is_counted_as_a_prefilter_miss(self) -> None:
        out = judge_cell(
            RoutedJudge(claims='{"claims_incomplete": [1]}'),
            summary_text="Only the first half of the call exists.",
            missed_items=[],
            slice_says_truncated=False,
            failure_input=False,
            seed=1,
        )
        assert out["b_fail"] is True
        assert out["b_prefilter_missed"] == 1

    def test_failure_input_adds_disclosure_labels_and_other_inputs_do_not(self) -> None:
        kw: dict[str, Any] = {
            "summary_text": "S.",
            "missed_items": [],
            "slice_says_truncated": False,
            "seed": 1,
        }
        judge = RoutedJudge(
            claims='{"claims_incomplete": []}',
            disclosure='{"disclosed": true, "absence_claim": true}',
        )
        failing = judge_cell(judge, failure_input=True, **kw)
        assert (failing["disclosed"], failing["absence_claim"]) == (True, True)
        assert "disclosed" not in judge_cell(judge, failure_input=False, **kw)

    def test_degraded_calls_are_listed(self) -> None:
        out = judge_cell(
            FakeJudge("garbage"),
            summary_text="S.",
            missed_items=ITEMS,
            slice_says_truncated=False,
            failure_input=True,
            seed=1,
        )
        assert out["degraded"] == ["recall", "claims", "disclosure"]

    def test_recall_conveyed_ids_come_back_sorted(self) -> None:
        out = judge_cell(
            RoutedJudge(
                recall=json.dumps({"conveyed": [1, 2]}), claims='{"claims_incomplete": []}'
            ),
            summary_text="S.",
            missed_items=ITEMS,
            slice_says_truncated=False,
            failure_input=False,
            seed=1,
        )
        assert out["recall_conveyed"] == ["g1", "g2"]
        assert out["recall_labels"] == {"g1": True, "g2": True}

    def test_labels_are_keyed_by_id_so_a_calibration_file_needs_no_prose(self) -> None:
        out = judge_cell(
            RoutedJudge(recall='{"conveyed": []}', claims='{"claims_incomplete": []}'),
            summary_text="The recording ends abruptly. Other things happened.",
            missed_items=ITEMS,
            slice_says_truncated=False,
            failure_input=False,
            seed=1,
        )
        assert out["recall_labels"] == {"g1": False, "g2": False}
        assert sentence_id("The recording ends abruptly.") in out["b_labels"]
        assert set(out["b_labels"].values()) == {False}


class TestNothingDecided:
    BASE: dict[str, Any] = {
        "summary_text": "No decisions were made. Pricing was discussed.",
        "missed_items": [],
        "slice_says_truncated": False,
        "failure_input": False,
        "seed": 1,
    }

    def test_an_emptied_decision_list_fails_c_without_asking_the_judge(self) -> None:
        judge = RoutedJudge(claims='{"claims_incomplete": []}')
        out = judge_cell(judge, empty_decisions=True, **self.BASE)
        assert out["c_fail"] is True

    def test_a_prefilter_hit_the_judge_confirms_fails_c(self) -> None:
        judge = RoutedJudge(
            claims='{"claims_incomplete": []}',
            nothing='{"claims_nothing_decided": [1]}',
        )
        out = judge_cell(judge, nothing_decided_hits=["No decisions were made."], **self.BASE)
        assert out["c_fail"] is True

    def test_a_prefilter_hit_the_judge_clears_leaves_c_passing(self) -> None:
        judge = RoutedJudge(
            claims='{"claims_incomplete": []}',
            nothing='{"claims_nothing_decided": []}',
        )
        out = judge_cell(judge, nothing_decided_hits=["No decision on colour."], **self.BASE)
        assert out["c_fail"] is False


class TestCalibration:
    def test_policy_bands_follow_the_plan_table(self) -> None:
        assert [judge_policy(k) for k in (0.9, 0.61, 0.6, 0.41, 0.4, 0.0)] == [
            "verdict",
            "verdict",
            "rank",
            "rank",
            "unused",
            "unused",
        ]

    def test_report_gives_kappa_confusion_and_the_allowed_use(self) -> None:
        human = {"c1": "yes", "c2": "no", "c3": "yes", "c4": "no", "c5": "yes", "c6": "no"}
        report = calibration_report(dict(human), human)
        assert report["cohens_kappa"] == 1.0
        assert report["policy"] == "verdict"
        flipped = {**human, "c1": "no", "c2": "yes", "c3": "no"}
        worse = calibration_report(flipped, human)
        assert worse["cohens_kappa"] < 0.41
        assert worse["policy"] == "unused"
        assert worse["confusion"]["judge=no|human=yes"] == 2

    def test_a_label_held_by_only_one_side_raises(self) -> None:
        with pytest.raises(ValueError, match="only one side"):
            calibration_report({"c1": "yes"}, {"c1": "yes", "c2": "no"})

    def test_export_strips_identity_shuffles_and_keeps_every_b_and_d_item(self) -> None:
        pool = [
            {
                "kind": "recall",
                "cell_id": f"I{1 + n % 3}|{'old' if n % 2 else 'new'}|{n}",
                "item_id": f"g{n}",
                "text": f"item {n}",
                "summary": f"summary {n}",
            }
            for n in range(30)
        ] + [
            {"kind": "b", "cell_id": "I1|new|0", "item_id": "s1", "text": "claim", "summary": "x"},
            {"kind": "d", "cell_id": "I4|old|2", "item_id": "-", "text": "", "summary": "y"},
        ]
        items, key = export_calibration(pool, recall_pairs=12, seed=7)
        assert sorted(key) == sorted(i["blind_id"] for i in items)
        assert len(items) == 14
        assert sum(1 for i in items if i["kind"] == "recall") == 12
        assert {"b", "d"} <= {i["kind"] for i in items}
        blob = json.dumps(items)
        assert "cell_id" not in blob  # nothing that names an arm, input or run reaches the labeller
        assert "|" not in blob
        assert key[items[0]["blind_id"]]["cell_id"].count("|") == 2
        again, _ = export_calibration(pool, recall_pairs=12, seed=7)
        assert again == items


def test_llm_service_judge_skips_the_token_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judge grades a benchmark from a host with no database: it must not ask the limit store."""
    from types import SimpleNamespace

    from app.services import llm_service
    from tests.eval.harness.summary_judge import llm_service_judge

    seen: list[dict[str, Any]] = []

    class _FakeService:
        def __init__(self, _config: Any) -> None:
            pass

        def chat_completion(self, _messages: list[dict[str, str]], **kwargs: Any) -> Any:
            seen.append(kwargs)
            return SimpleNamespace(content="{}")

    monkeypatch.setattr(llm_service, "LLMService", _FakeService)
    config = SimpleNamespace(
        provider=SimpleNamespace(value="anthropic"), model="claude-haiku-5-5", base_url=None
    )

    judge, _provenance = llm_service_judge(config)
    judge("grade this")

    assert seen and seen[0].get("enforce_budget") is False
