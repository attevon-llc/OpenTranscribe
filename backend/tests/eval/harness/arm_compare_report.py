"""Deterministic JSON and Markdown rendering for :mod:`tests.eval.harness.arm_compare`.

Split out so the comparison logic stays free of formatting. Both renderers take the
metrics-only document ``build_report`` returns and add nothing to it, so the Markdown can
carry no prose the JSON does not.
"""

from __future__ import annotations

from typing import Any


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def render_markdown(report: dict[str, Any]) -> str:
    """The comparison as GitHub-flavoured Markdown. Byte-identical for equal input."""
    decision = report["decision"]
    th = report["thresholds"]
    lines = [
        "# #532 arm comparison",
        "",
        f"**Verdict: {decision['verdict']}**",
        "",
        f"- category: `{report['category']}`; baseline: `{report['baseline']}`",
        f"- bootstrap: {th['n_resamples']} resamples, seed {th['seed']}, "
        f"{int(th['confidence'] * 100)}% CI",
        f"- rule: USED delta >= {th['used_min_delta']:+.2f} with CI lower bound > 0; "
        f"content CI lower bound > {th['content_non_inferiority']:+.2f}",
        f"- composition counters checked: {report['composition_checked']}",
        "",
        "## Arms",
        "",
        "| arm | turns | in scope | cited | offered | mean USED | pooled USED | mean OFFERED "
        "| mean content | content turns |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for role, arm in report["arms"].items():
        lines.append(
            f"| {role} | {arm['turns']} | {arm['files_in_scope']} | {arm['files_cited']} "
            f"| {arm['files_offered']} | {_fmt(arm['mean_used'])} | {_fmt(arm['pooled_used'])} "
            f"| {_fmt(arm['mean_offered'])} | {_fmt(arm['mean_content'])} "
            f"| {arm['content_turns']} |"
        )
    lines += [
        "",
        "## Paired differences (second minus first)",
        "",
        "| comparison | n | delta | CI low | CI high |",
        "|---|---|---|---|---|",
    ]
    for name, comp in report["comparisons"].items():
        if comp["n"] == 0:
            lines.append(f"| {name} | 0 | n/a | n/a | n/a |")
            continue
        lines.append(
            f"| {name} | {comp['n']} | {comp['delta_mean']:+.4f} "
            f"| {comp['ci_low']:+.4f} | {comp['ci_high']:+.4f} |"
        )
    lines += ["", "## Decision", ""]
    for name, ok in decision["criteria"].items():
        lines.append(f"- {name}: {'not evaluated' if ok is None else ok}")
    lines += [f"- reason: {r}" for r in decision["reasons"]]
    lines += [f"- void: {r}" for r in decision["void_reasons"]]
    violations = report["applied_check_violations"]
    if violations:
        lines += [
            "",
            "## Applied-check violations",
            "",
            "| arm | query_id | code |",
            "|---|---|---|",
        ]
        lines += [
            f"| {role} | {v['query_id']} | {v['code']} |"
            for role, vs in violations.items()
            for v in vs
        ]
    return "\n".join(lines) + "\n"
