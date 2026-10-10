"""#532 U7: ``harness/arm_compare.py`` and ``scripts/compare_probe_arms.py``.

Synthetic probe fixtures only, shaped like one entry of ``probe_chat_rag.py``'s
``results.json``. Each case builds whole arms whose USED coverage is controlled
exactly (an arm "cites the first k of the 20 scope files"), so every expected delta is a
known fraction rather than whatever a sample happened to give. The decision rule is checked
from both sides: a case that must pass, cases that must fail for each separate reason, the
exact threshold, and a must-not-be-fooled case for each field the metric must ignore.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.eval.harness import arm_compare as ac
from tests.eval.harness import arm_compare_report as acr
from tests.eval.harness.report import dumps

pytestmark = pytest.mark.unit

SCOPE = [f"file-{i:02d}" for i in range(20)]
REFERENCE = "[R1] budget forecast approved\n[R2] colour scheme selected"
GOOD_ANSWER = "SECRET-ANSWER the budget forecast was approved and a colour scheme selected"
BAD_ANSWER = "SECRET-ANSWER nothing relevant was said"
N_TURNS = 30


def _meta(role: str, *, cache_hit: bool = False) -> dict[str, Any]:
    overview: dict[str, Any] = {
        "reducer": "code",
        "truncated": False,
        "files_listed": 20,
        "entries_hybrid": 20 if role == ac.ROLE_HYBRID else 0,
        # Hybrid-branch-only in the code that ran: a correctly applied arm D reports 0 here.
        "entries_summary_only": 0,
        "entries_digest": 20 if role in (ac.ROLE_CONTROL, ac.ROLE_REPEAT) else 0,
        "summary_chars": 0 if role in (ac.ROLE_CONTROL, ac.ROLE_REPEAT) else 9_000,
    }
    return {"budget_chars": 175_000, "cache_hit": cache_hit, "overview": overview}


def _record(
    index: int,
    role: str,
    cited: list[str],
    *,
    answer: str = GOOD_ANSWER,
    reference: str = REFERENCE,
    offered: list[str] | None = None,
    consulted: list[str] | None = None,
    cache_hit: bool = False,
) -> dict[str, Any]:
    offered_files = SCOPE if offered is None else offered
    return {
        "label": f"multi-{index:03d}",
        "category": "multi_file",
        "question": "SECRET-QUESTION what was decided",
        "reference_answer": reference,
        "scope_file_uuids": SCOPE,
        "expect_refusal": False,
        "app_answer": answer,
        "error": None,
        "warnings": [],
        "msg_metadata": _meta(role, cache_hit=cache_hit),
        "citations": [{"file_uuid": u, "snippet": "SECRET-SNIPPET"} for u in cited],
        "offered_citations": [{"id": n, "file_uuid": u} for n, u in enumerate(offered_files)],
        "files_consulted_uuids": SCOPE[:1] if consulted is None else consulted,
    }


def _arm(role: str, counts: list[int], **kwargs: Any) -> list[dict[str, Any]]:
    """An arm that, on turn i, cites the first ``counts[i]`` files of the scope."""
    return [_record(i, role, SCOPE[:k], **kwargs) for i, k in enumerate(counts)]


def _base_counts() -> list[int]:
    return [8 + (i % 5) for i in range(N_TURNS)]


def _plus(counts: list[int], gains: list[int]) -> list[int]:
    return [c + g for c, g in zip(counts, gains, strict=True)]


def _report(**arms: list[dict[str, Any]]) -> dict[str, Any]:
    return ac.build_report({role.replace("_", "-"): recs for role, recs in arms.items()})


def test_clear_improvement_wins() -> None:
    base = _base_counts()
    gains = [2 + (i % 3) for i in range(N_TURNS)]  # +2..+4 files of 20 = +10..+20 points
    report = _report(control=_arm("control", base), hybrid=_arm("hybrid", _plus(base, gains)))
    used = report["comparisons"]["hybrid_vs_baseline_used"]
    assert report["decision"]["verdict"] == ac.VERDICT_WIN
    assert used["delta_mean"] == pytest.approx(0.15, abs=0.01)
    assert used["ci_low"] > 0.10
    assert report["decision"]["reasons"] == []


def test_no_improvement_fails_and_a_null_pair_has_an_exactly_zero_ci() -> None:
    base = _base_counts()
    report = _report(control=_arm("control", base), hybrid=_arm("hybrid", base))
    used = report["comparisons"]["hybrid_vs_baseline_used"]
    assert report["decision"]["verdict"] == ac.VERDICT_FAIL
    assert (used["delta_mean"], used["ci_low"], used["ci_high"]) == (0.0, 0.0, 0.0)
    assert "used_coverage_below_threshold_or_ci_includes_zero" in report["decision"]["reasons"]


def test_exactly_five_points_passes() -> None:
    base = _base_counts()
    report = _report(
        control=_arm("control", base), hybrid=_arm("hybrid", _plus(base, [1] * N_TURNS))
    )
    used = report["comparisons"]["hybrid_vs_baseline_used"]
    assert used["delta_mean"] == pytest.approx(0.05, abs=1e-9)
    assert report["decision"]["verdict"] == ac.VERDICT_WIN


def test_just_under_five_points_fails_on_the_threshold_not_the_ci() -> None:
    base = _base_counts()
    gains = [1] * (N_TURNS - 1) + [0]  # mean 29/30 * 5 points = 4.83 points
    report = _report(control=_arm("control", base), hybrid=_arm("hybrid", _plus(base, gains)))
    used = report["comparisons"]["hybrid_vs_baseline_used"]
    assert used["ci_low"] > 0
    assert used["delta_mean"] < ac.USED_MIN_DELTA
    assert report["decision"]["verdict"] == ac.VERDICT_FAIL


def test_mean_above_threshold_but_ci_crossing_zero_fails() -> None:
    base = [6] * 6
    gains = [8, -2, 8, -2, 8, -2]  # mean +3 files = +15 points, but two thirds of the sign mass
    report = ac.build_report(
        {
            "control": _arm("control", base)[:6],
            "hybrid": _arm("hybrid", _plus(base, gains))[:6],
        }
    )
    used = report["comparisons"]["hybrid_vs_baseline_used"]
    assert used["delta_mean"] >= ac.USED_MIN_DELTA
    assert used["ci_low"] <= 0
    assert report["decision"]["verdict"] == ac.VERDICT_FAIL


def test_files_consulted_never_moves_the_verdict() -> None:
    base = _base_counts()
    everything = list(SCOPE)
    # The derived field claims the hybrid cited all 20 files and the control cited none.
    # The persisted citations show no difference at all.
    lying = ac.build_report(
        {
            "control": _arm("control", base, consulted=[]),
            "hybrid": _arm("hybrid", base, consulted=everything),
        }
    )
    assert lying["decision"]["verdict"] == ac.VERDICT_FAIL
    assert lying["comparisons"]["hybrid_vs_baseline_used"]["delta_mean"] == 0.0
    # And the reverse: a real gain is not lost because the derived field is empty.
    gains = [3] * N_TURNS
    honest = ac.build_report(
        {
            "control": _arm("control", base, consulted=everything),
            "hybrid": _arm("hybrid", _plus(base, gains), consulted=[]),
        }
    )
    assert honest["decision"]["verdict"] == ac.VERDICT_WIN
    assert honest["comparisons"]["hybrid_vs_baseline_used"]["delta_mean"] == pytest.approx(0.15)


def test_offered_is_read_from_offered_citations_not_from_cited_files() -> None:
    base = _base_counts()
    offered_ten = SCOPE[:10]
    report = ac.build_report(
        {
            "control": _arm("control", base, offered=offered_ten, consulted=SCOPE),
            "hybrid": _arm("hybrid", base, offered=SCOPE, consulted=[]),
        }
    )
    assert report["arms"]["control"]["mean_offered"] == 0.5
    assert report["arms"]["hybrid"]["mean_offered"] == 1.0
    assert report["comparisons"]["hybrid_vs_baseline_offered"]["delta_mean"] == 0.5


def test_citation_outside_the_scope_is_not_counted() -> None:
    records = [_record(0, "hybrid", [*SCOPE[:5], "file-elsewhere"])]
    turn = ac.score_turn(records[0])
    assert (turn.files_cited, turn.files_in_scope, turn.used) == (5, 20, 0.25)


def test_losing_content_coverage_fails_even_when_citations_rise() -> None:
    base = _base_counts()
    report = _report(
        control=_arm("control", base),
        hybrid=_arm("hybrid", _plus(base, [3] * N_TURNS), answer=BAD_ANSWER),
    )
    content = report["comparisons"]["hybrid_vs_baseline_content"]
    assert content["delta_mean"] == -1.0
    reasons = report["decision"]["reasons"]
    assert report["decision"]["verdict"] == ac.VERDICT_FAIL
    assert "content_coverage_lost" in reasons
    assert "divergence_used_up_content_down_escalate" in reasons


def test_unmeasurable_content_is_a_failure_not_a_pass() -> None:
    base = _base_counts()
    untagged = "budget forecast approved"
    report = _report(
        control=_arm("control", base, reference=untagged),
        hybrid=_arm("hybrid", _plus(base, [3] * N_TURNS), reference=untagged),
    )
    assert report["comparisons"]["hybrid_vs_baseline_content"]["n"] == 0
    assert report["decision"]["verdict"] == ac.VERDICT_FAIL
    assert "content_coverage_not_measurable" in report["decision"]["reasons"]


def test_arm_d_sanity_must_be_beaten_for_a_win() -> None:
    base = _base_counts()
    hybrid = _arm("hybrid", _plus(base, [2] * N_TURNS))
    beaten = _report(
        control=_arm("control", base),
        hybrid=hybrid,
        arm_d=_arm("arm-d", base, answer=BAD_ANSWER),
    )
    assert beaten["decision"]["criteria"]["sanity_hybrid_beats_arm_d"] is True
    assert beaten["decision"]["verdict"] == ac.VERDICT_WIN
    stronger_d = _report(
        control=_arm("control", base),
        hybrid=hybrid,
        arm_d=_arm("arm-d", _plus(base, [4] * N_TURNS)),
    )
    assert stronger_d["decision"]["verdict"] == ac.VERDICT_FAIL
    assert "hybrid_does_not_beat_arm_d" in stronger_d["decision"]["reasons"]


def test_baseline_is_the_mean_of_both_controls() -> None:
    base = _base_counts()
    report = _report(
        control=_arm("control", base),
        repeat_control=_arm("control", _plus(base, [1] * N_TURNS)),
        hybrid=_arm("hybrid", _plus(base, [3] * N_TURNS)),
    )
    # Hybrid is +3 over the control and +2 over the repeat: +2.5 files over their mean.
    used = report["comparisons"]["hybrid_vs_baseline_used"]
    assert used["delta_mean"] == pytest.approx(0.125)
    assert report["comparisons"]["aa_used"]["delta_mean"] == pytest.approx(0.05)


def test_controls_that_disagree_void_the_window() -> None:
    base = _base_counts()
    report = _report(
        control=_arm("control", base),
        repeat_control=_arm("control", _plus(base, [4] * N_TURNS)),
        hybrid=_arm("hybrid", _plus(base, [6] * N_TURNS)),
    )
    assert report["decision"]["verdict"] == ac.VERDICT_VOID
    assert "aa_used_ci_excludes_zero" in report["decision"]["void_reasons"]


@pytest.mark.parametrize(
    ("role", "kwargs", "code"),
    [
        ("control", {"cache_hit": True}, "cache_hit"),
        ("hybrid", {"cache_hit": True}, "cache_hit"),
    ],
)
def test_cache_hit_voids_an_arm(role: str, kwargs: dict[str, Any], code: str) -> None:
    base = _base_counts()
    arms = {
        "control": _arm("control", base),
        "hybrid": _arm("hybrid", _plus(base, [3] * N_TURNS)),
    }
    arms[role] = _arm(role, [c + (3 if role == "hybrid" else 0) for c in base], **kwargs)
    report = ac.build_report(arms)
    codes = [v["code"] for v in report["applied_check_violations"][role]]
    assert report["decision"]["verdict"] == ac.VERDICT_VOID
    assert set(codes) == {code}
    assert len(codes) == N_TURNS


def test_hybrid_arm_without_hybrid_entries_is_void_unless_composition_is_waived() -> None:
    base = _base_counts()
    hybrid = _arm("hybrid", _plus(base, [3] * N_TURNS))
    for record in hybrid:
        record["msg_metadata"]["overview"]["entries_hybrid"] = 0
    arms = {"control": _arm("control", base), "hybrid": hybrid}
    strict = ac.build_report(arms)
    assert strict["decision"]["verdict"] == ac.VERDICT_VOID
    waived = ac.build_report(arms, check_composition=False)
    assert waived["decision"]["verdict"] == ac.VERDICT_WIN


def _void_codes(arm_d: list[dict[str, Any]]) -> list[str]:
    base = _base_counts()
    report = _report(
        control=_arm("control", base),
        hybrid=_arm("hybrid", _plus(base, [3] * N_TURNS)),
        arm_d=arm_d,
    )
    return sorted({v["code"] for v in report["applied_check_violations"].get("arm-d", [])})


def test_a_correctly_applied_arm_d_is_not_void() -> None:
    """MUST-STAY-CLEAN: arm D reports `entries_summary_only == 0` (that counter is
    hybrid-branch-only) and must not be voided for it."""
    arm_d = _arm("arm-d", _base_counts())
    assert arm_d[0]["msg_metadata"]["overview"]["entries_summary_only"] == 0
    assert _void_codes(arm_d) == []


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("summary_chars", 0, "arm_d_no_summary_text"),
        ("entries_digest", 3, "arm_d_digest_fallback"),
        ("entries_hybrid", 1, "hybrid_entries_in_arm_d"),
    ],
)
def test_arm_d_that_was_not_applied_is_void(field: str, value: int, code: str) -> None:
    """MUST-FIRE: each way arm D can fail to be the shipped summary tier."""
    arm_d = _arm("arm-d", _base_counts())
    arm_d[5]["msg_metadata"]["overview"][field] = value
    report = _report(
        control=_arm("control", _base_counts()),
        hybrid=_arm("hybrid", _plus(_base_counts(), [3] * N_TURNS)),
        arm_d=arm_d,
    )
    assert report["applied_check_violations"]["arm-d"] == [{"query_id": "multi-005", "code": code}]
    assert report["decision"]["verdict"] == ac.VERDICT_VOID


def test_arm_d_with_no_summary_counters_at_all_is_void() -> None:
    """A record that never reported the counters proves nothing was applied."""
    arm_d = _arm("arm-d", _base_counts())
    for record in arm_d:
        del record["msg_metadata"]["overview"]["summary_chars"]
    assert _void_codes(arm_d) == ["arm_d_no_summary_text"]


def test_small_context_window_is_void() -> None:
    base = _base_counts()
    control = _arm("control", base)
    control[3]["msg_metadata"]["budget_chars"] = 15_000
    report = ac.build_report({"control": control, "hybrid": _arm("hybrid", base)})
    assert report["applied_check_violations"]["control"] == [
        {"query_id": "multi-003", "code": "budget_chars"}
    ]
    assert report["decision"]["verdict"] == ac.VERDICT_VOID


def test_arms_over_different_questions_are_refused() -> None:
    base = _base_counts()
    with pytest.raises(ac.ArmDataError, match="graded labels differ"):
        ac.build_report({"control": _arm("control", base)[:-1], "hybrid": _arm("hybrid", base)})


def test_a_missing_required_role_is_refused() -> None:
    with pytest.raises(ac.ArmDataError, match="missing"):
        ac.build_report({"control": _arm("control", _base_counts())})


def test_report_is_deterministic_and_carries_no_prose() -> None:
    base = _base_counts()
    arms = {
        "control": _arm("control", base),
        "hybrid": _arm("hybrid", _plus(base, [2 + (i % 3) for i in range(N_TURNS)])),
    }
    first, second = ac.build_report(arms), ac.build_report(arms)
    rendered = dumps(first) + acr.render_markdown(first)
    assert dumps(first) == dumps(second)
    assert acr.render_markdown(first) == acr.render_markdown(second)
    leaked = [s for s in ("SECRET-QUESTION", "SECRET-ANSWER", "SECRET-SNIPPET") if s in rendered]
    assert leaked == []
    assert "Verdict: WIN" in rendered


def _cli() -> Any:
    path = Path(__file__).resolve().parents[3] / "scripts" / "compare_probe_arms.py"
    spec = importlib.util.spec_from_file_location("compare_probe_arms_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_arm(root: Path, name: str, records: list[dict[str, Any]]) -> str:
    directory = root / name
    directory.mkdir()
    (directory / "results.json").write_text(json.dumps(records), encoding="utf-8")
    return f"{name}={directory}"


def test_cli_exit_codes_and_artifacts(tmp_path: Path) -> None:
    cli = _cli()
    base = _base_counts()
    control = _write_arm(tmp_path, "control", _arm("control", base))
    winner = _write_arm(tmp_path, "hybrid", _arm("hybrid", _plus(base, [3] * N_TURNS)))
    out = tmp_path / "out"
    assert cli.main(["--arm", control, "--arm", winner, "--out", str(out)]) == 0
    written = json.loads((out / "arm_compare.json").read_text(encoding="utf-8"))
    assert written["decision"]["verdict"] == "WIN"
    assert (out / "arm_compare.md").read_text(encoding="utf-8").startswith("# #532 arm comparison")

    tie = tmp_path / "tie"
    tie.mkdir()
    same = _write_arm(tie, "hybrid", _arm("hybrid", base))
    assert cli.main(["--arm", control, "--arm", same, "--out", str(tie / "o")]) == 1

    bad = _arm("hybrid", base, cache_hit=True)
    void = _write_arm(tmp_path, "void", bad).replace("void=", "hybrid=")
    assert cli.main(["--arm", control, "--arm", void, "--out", str(tmp_path / "v")]) == 3

    assert cli.main(["--arm", "control", "--out", str(tmp_path / "x")]) == 2
