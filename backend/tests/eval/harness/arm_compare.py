"""Paired arm comparison for the #532 hybrid-summary measurement (plan unit U7).

Input is two or more probe runs (``scripts/probe_chat_rag.py``'s full-fidelity
``results.json``, one list of per-question records per arm). Output is a metrics-only
document: the per-arm USED / OFFERED / content coverage, the paired per-question
differences with a seeded bootstrap 95% CI, the applied-checks, and the pre-registered
pass/fail decision from ``docs/design/532_hybrid_summary_synthesis_plan.md`` section 5.2.

⚠️ **Which field is which, because the three look alike and were conflated before.**

* ``files_in_scope`` is the record's ``scope_file_uuids``.
* **USED** (``files_cited``) is the distinct in-scope files among the persisted
  ``citations`` the answer actually carries. It is NOT read from ``files_consulted_uuids``:
  that is a derived field of the same record, and the decision must not depend on a
  value another layer computed (a record with a stale or wrong ``files_consulted_uuids``
  must not move the verdict).
* **OFFERED** is the distinct in-scope files among ``offered_citations`` (what retrieval and
  the budget delivered). It is never taken from ``files_consulted_uuids``, which is what the
  model chose to cite.

Both are intersected with the scope, so an out-of-scope citation cannot push a ratio past
what the question asked for.

Everything emitted is a count, a ratio, a label, or a code. Question text, reference
answers, answer prose and snippets are read (content coverage scores the answer against
the reference) but never copied out; :func:`probe_metrics.assert_no_prose` re-checks the
assembled document.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.eval.harness import ami_recall
from tests.eval.harness.probe_metrics import assert_no_prose
from tests.eval.harness.significance import paired_bootstrap_ci

#: Arm roles, and which flags each one is declared to have run with.
ROLE_CONTROL = "control"
ROLE_REPEAT = "repeat-control"
ROLE_ARM_D = "arm-d"
ROLE_HYBRID = "hybrid"
ROLES = (ROLE_CONTROL, ROLE_REPEAT, ROLE_ARM_D, ROLE_HYBRID)

DEFAULT_CATEGORY = "multi_file"
#: Plan 5.2: H - C-bar on USED must be at least 5 points...
USED_MIN_DELTA = 0.05
#: ...and the content-coverage CI lower bound must stay above -3 points.
CONTENT_NON_INFERIORITY = -0.03
#: Plan 4.2: 20,000 resamples, seed 0.
N_RESAMPLES = 20_000
SEED = 0
#: Plan 4.3 / CW-1: a budget at or below this means a small-window model was measured.
BUDGET_CHARS_FLOOR = 100_000
#: Median share of listed files carrying a summary entry, for arms D and H.
MIN_MEDIAN_ENTRY_SHARE = 0.95
#: Float slack for comparing a mean of ratios to a threshold written as a decimal.
_EPS = 1e-9

VERDICT_WIN = "WIN"
VERDICT_FAIL = "FAIL"
VERDICT_VOID = "VOID"


class ArmDataError(ValueError):
    """The input cannot be compared: malformed records, or arms that do not line up."""


@dataclass(frozen=True)
class TurnScore:
    """One graded turn's metrics. Counts and ratios only."""

    query_id: str
    files_in_scope: int
    files_cited: int
    files_offered: int
    used: float
    offered: float
    content: float | None
    scope: frozenset[str]


def load_records(path: Path) -> list[dict[str, Any]]:
    """Read one arm's ``results.json`` (a file, or the directory that holds it)."""
    target = path / "results.json" if path.is_dir() else path
    records = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ArmDataError(f"{target}: expected a list of per-question records")
    return records


def _file_uuids(refs: list[dict[str, Any]]) -> set[str]:
    return {
        str(ref.get("file_uuid") or ref.get("fileUuid"))
        for ref in refs
        if ref.get("file_uuid") or ref.get("fileUuid")
    }


def content_coverage(record: dict[str, Any]) -> float | None:
    """Recordings with at least one reference item recalled / recordings tagged.

    ``None`` when the reference carries no ``[recording]`` tags: there is nothing to
    cover, and a 0.0 would read as a measured zero.
    """
    reference = record.get("reference_answer") or ""
    tagged = {rec for rec, _ in ami_recall.parse_reference_items(reference) if rec}
    if not tagged:
        return None
    score = ami_recall.score_answer(record.get("app_answer") or "", reference)
    return len(score.recordings_covered() & tagged) / len(tagged)


def score_turn(record: dict[str, Any]) -> TurnScore:
    """Score one record. See the module docstring for which fields feed which number."""
    label = str(record.get("label", "<unlabelled>"))
    if "citations" not in record or record.get("offered_citations") is None:
        raise ArmDataError(f"{label}: record lacks citations/offered_citations")
    scope = frozenset(str(u) for u in record.get("scope_file_uuids") or [])
    if not scope:
        raise ArmDataError(f"{label}: empty scope, coverage is undefined")
    cited = _file_uuids(record["citations"]) & scope
    offered = _file_uuids(record["offered_citations"]) & scope
    return TurnScore(
        query_id=label,
        files_in_scope=len(scope),
        files_cited=len(cited),
        files_offered=len(offered),
        used=len(cited) / len(scope),
        offered=len(offered) / len(scope),
        content=content_coverage(record),
        scope=scope,
    )


def select_turns(records: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    """The graded turns: the category, minus negative controls, sorted by label."""
    chosen = [
        r for r in records if r.get("category") == category and not r.get("expect_refusal", False)
    ]
    labels = [r.get("label") for r in chosen]
    if len(set(labels)) != len(labels):
        raise ArmDataError("duplicate labels among graded turns")
    return sorted(chosen, key=lambda r: str(r.get("label")))


def applied_checks(
    role: str, turns: list[dict[str, Any]], *, check_composition: bool = True
) -> list[dict[str, str]]:
    """Plan 4.3: reasons an arm is void. Each is ``{query_id, code}``; empty means clean."""
    found: list[dict[str, str]] = []

    def flag(query_id: Any, code: str) -> None:
        found.append({"query_id": str(query_id), "code": code})

    shares: list[float] = []
    for turn in turns:
        label = turn.get("label")
        meta = turn.get("msg_metadata") or {}
        overview = meta.get("overview") or {}
        if turn.get("error") is not None:
            flag(label, "error")
        if any((w or {}).get("code") == "provider_error" for w in turn.get("warnings") or []):
            flag(label, "provider_error")
        if meta.get("cache_hit"):
            flag(label, "cache_hit")
        if (meta.get("budget_chars") or 0) <= BUDGET_CHARS_FLOOR:
            flag(label, "budget_chars")
        if not check_composition:
            continue
        if overview.get("truncated"):
            flag(label, "overview_truncated")
        if overview.get("reducer") != "code":
            flag(label, "overview_reducer")
        hybrid = overview.get("entries_hybrid", 0)
        summary_only = overview.get("entries_summary_only", 0)
        listed = overview.get("files_listed") or 1
        if role in (ROLE_CONTROL, ROLE_REPEAT) and (hybrid or summary_only):
            flag(label, "control_has_summary_entries")
        if role == ROLE_ARM_D:
            shares.append(summary_only / listed)
            if hybrid:
                flag(label, "hybrid_entries_in_arm_d")
        if role == ROLE_HYBRID:
            shares.append(hybrid / listed)
            if not hybrid:
                flag(label, "no_hybrid_entry")
    if shares and statistics.median(shares) < MIN_MEDIAN_ENTRY_SHARE:
        flag("*", "median_entry_share")
    return found


def _ci(deltas: list[float]) -> dict[str, Any]:
    boot = paired_bootstrap_ci(deltas, n_resamples=N_RESAMPLES, seed=SEED)
    return {
        "n": boot.n,
        "delta_mean": round(boot.delta_mean, 6),
        "ci_low": round(boot.ci_low, 6),
        "ci_high": round(boot.ci_high, 6),
        "n_resamples": boot.n_resamples,
        "seed": SEED,
    }


def _align(a: dict[str, TurnScore], b: dict[str, TurnScore], names: tuple[str, str]) -> list[str]:
    if a.keys() != b.keys():
        raise ArmDataError(
            f"{names[0]} vs {names[1]}: graded labels differ "
            f"(only in first: {sorted(a.keys() - b.keys())[:3]}, "
            f"only in second: {sorted(b.keys() - a.keys())[:3]})"
        )
    for label in a:
        if a[label].scope != b[label].scope:
            raise ArmDataError(f"{names[0]} vs {names[1]}: scope differs for {label}")
    return sorted(a)


def compare(
    a: dict[str, TurnScore], b: dict[str, TurnScore], metric: str, names: tuple[str, str]
) -> dict[str, Any]:
    """Paired ``b - a`` on ``metric`` (``used``/``offered``/``content``) with a bootstrap CI.

    Content is compared only over turns where it is defined in both arms; ``n`` says how
    many that was. ``None`` when no turn qualifies.
    """
    labels = _align(a, b, names)
    deltas = [
        getattr(b[k], metric) - getattr(a[k], metric)
        for k in labels
        if getattr(a[k], metric) is not None and getattr(b[k], metric) is not None
    ]
    if not deltas:
        return {"n": 0, "metric": metric, "pair": list(names)}
    return {"metric": metric, "pair": list(names), **_ci(deltas)}


def baseline_scores(
    control: dict[str, TurnScore], repeat: dict[str, TurnScore] | None
) -> dict[str, TurnScore]:
    """C-bar: the per-turn mean of the two controls (just the control if only one ran)."""
    if repeat is None:
        return control
    labels = _align(control, repeat, (ROLE_CONTROL, ROLE_REPEAT))

    def mean(x: float | None, y: float | None) -> float | None:
        return None if x is None or y is None else (x + y) / 2

    return {
        k: TurnScore(
            query_id=k,
            files_in_scope=control[k].files_in_scope,
            files_cited=control[k].files_cited,
            files_offered=control[k].files_offered,
            used=(control[k].used + repeat[k].used) / 2,
            offered=(control[k].offered + repeat[k].offered) / 2,
            content=mean(control[k].content, repeat[k].content),
            scope=control[k].scope,
        )
        for k in labels
    }


def _excludes_zero(result: dict[str, Any]) -> bool:
    if result["n"] == 0:
        return False
    return bool(result["ci_low"] > 0 or result["ci_high"] < 0)


def decide(comparisons: dict[str, dict[str, Any]], violations: dict[str, list]) -> dict[str, Any]:
    """Apply plan 5.2 (and the void / sanity rules of section 1 of the runbook).

    WIN needs all of: USED delta >= +0.05 with CI lower bound > 0, and content-coverage CI
    lower bound > -0.03. Content that cannot be measured fails the criterion; it is never
    waved through. A void window (an arm failed an applied-check, or the A/A pair differs)
    is not graded at all.
    """
    reasons: list[str] = []
    void = [f"{role}:{v['code']}" for role, vs in sorted(violations.items()) for v in vs]
    aa = comparisons.get("aa_used")
    if aa is not None and _excludes_zero(aa):
        void.append("aa_used_ci_excludes_zero")
    if void:
        return {"verdict": VERDICT_VOID, "void_reasons": void, "reasons": [], "criteria": {}}

    used, content = (
        comparisons["hybrid_vs_baseline_used"],
        comparisons["hybrid_vs_baseline_content"],
    )
    used_ok = used["delta_mean"] >= USED_MIN_DELTA - _EPS and used["ci_low"] > 0
    content_measured = content["n"] > 0
    content_ok = content_measured and content["ci_low"] > CONTENT_NON_INFERIORITY
    sanity: bool | None = None
    if "hybrid_vs_arm_d_used" in comparisons:
        sanity = (
            comparisons["hybrid_vs_arm_d_used"]["ci_low"] > 0
            and comparisons["hybrid_vs_arm_d_content"].get("ci_low", -1.0) > 0
        )
    if not used_ok:
        reasons.append("used_coverage_below_threshold_or_ci_includes_zero")
    if not content_measured:
        reasons.append("content_coverage_not_measurable")
    elif not content_ok:
        reasons.append("content_coverage_lost")
    if sanity is False:
        reasons.append("hybrid_does_not_beat_arm_d")
    if used_ok and content_measured and not content_ok:
        reasons.append("divergence_used_up_content_down_escalate")
    criteria = {
        "used_delta_at_least_5_points_ci_low_above_zero": used_ok,
        "content_ci_low_above_minus_3_points": content_ok,
        "sanity_hybrid_beats_arm_d": sanity,
    }
    verdict = VERDICT_WIN if used_ok and content_ok and sanity is not False else VERDICT_FAIL
    return {"verdict": verdict, "void_reasons": [], "reasons": reasons, "criteria": criteria}


def _arm_summary(scores: dict[str, TurnScore]) -> dict[str, Any]:
    rows = list(scores.values())
    in_scope = sum(r.files_in_scope for r in rows)
    contents = [r.content for r in rows if r.content is not None]
    return {
        "turns": len(rows),
        "files_in_scope": in_scope,
        "files_cited": sum(r.files_cited for r in rows),
        "files_offered": sum(r.files_offered for r in rows),
        "mean_used": round(sum(r.used for r in rows) / len(rows), 6),
        "pooled_used": round(sum(r.files_cited for r in rows) / in_scope, 6),
        "mean_offered": round(sum(r.offered for r in rows) / len(rows), 6),
        "mean_content": round(sum(contents) / len(contents), 6) if contents else None,
        "content_turns": len(contents),
    }


def build_report(
    arms: dict[str, list[dict[str, Any]]],
    *,
    category: str = DEFAULT_CATEGORY,
    check_composition: bool = True,
) -> dict[str, Any]:
    """The metrics-only comparison document, deterministic for a given input.

    Args:
        arms: role -> that arm's raw probe records. Must hold ``control`` and ``hybrid``;
            ``repeat-control`` and ``arm-d`` are optional.
        category: The graded query category.
        check_composition: Run the overview-counter applied-checks. Off only for arms that
            predate the counters.

    Raises:
        ArmDataError: A required role is missing, an unknown role is given, or the arms
            do not share the same graded questions and scopes.
    """
    unknown = sorted(set(arms) - set(ROLES))
    missing = [r for r in (ROLE_CONTROL, ROLE_HYBRID) if r not in arms]
    if unknown or missing:
        raise ArmDataError(f"roles: unknown {unknown}, missing {missing}; valid {list(ROLES)}")
    turns = {role: select_turns(records, category) for role, records in arms.items()}
    if any(not t for t in turns.values()):
        raise ArmDataError(f"an arm has no graded '{category}' turns")
    scores = {role: {str(t["label"]): score_turn(t) for t in ts} for role, ts in turns.items()}
    violations = {
        role: applied_checks(role, ts, check_composition=check_composition)
        for role, ts in turns.items()
    }
    base = baseline_scores(scores[ROLE_CONTROL], scores.get(ROLE_REPEAT))
    hyb = scores[ROLE_HYBRID]
    comps: dict[str, dict[str, Any]] = {}
    for metric in ("used", "content", "offered"):
        comps[f"hybrid_vs_baseline_{metric}"] = compare(
            base, hyb, metric, ("baseline", ROLE_HYBRID)
        )
    if ROLE_REPEAT in scores:
        for metric in ("used", "content"):
            comps[f"aa_{metric}"] = compare(
                scores[ROLE_CONTROL], scores[ROLE_REPEAT], metric, (ROLE_CONTROL, ROLE_REPEAT)
            )
    if ROLE_ARM_D in scores:
        for metric in ("used", "content"):
            comps[f"hybrid_vs_arm_d_{metric}"] = compare(
                scores[ROLE_ARM_D], hyb, metric, (ROLE_ARM_D, ROLE_HYBRID)
            )
            comps[f"arm_d_vs_baseline_{metric}"] = compare(
                base, scores[ROLE_ARM_D], metric, ("baseline", ROLE_ARM_D)
            )
    report = {
        "schema_version": 1,
        "category": category,
        "thresholds": {
            "used_min_delta": USED_MIN_DELTA,
            "content_non_inferiority": CONTENT_NON_INFERIORITY,
            "n_resamples": N_RESAMPLES,
            "seed": SEED,
            "confidence": 0.95,
        },
        "baseline": "mean(control, repeat-control)" if ROLE_REPEAT in scores else "control",
        "composition_checked": check_composition,
        "arms": {role: _arm_summary(s) for role, s in sorted(scores.items())},
        "comparisons": comps,
        "applied_check_violations": {r: v for r, v in sorted(violations.items()) if v},
        "decision": decide(comps, violations),
    }
    assert_no_prose(report)
    return report
