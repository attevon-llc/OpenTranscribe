"""`metrics.json` / `metrics.md`: the committed, prose-free summary of a run.

Everything here is derived from scored rows, never from record text. `assert_no_prose` runs on
the payload before it is written, so a future edit that leaks a summary or a gold item fails
loudly instead of reaching a public repo. Full-fidelity records stay outside the repo.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from tests.eval.harness.probe_metrics import assert_no_prose
from tests.eval.harness.summary_verdict import RECALL_METRICS

from summary_bench.config import HARNESS_VERSION

SCHEMA = 1


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (input, arm): cell counts, mean recall, and the failure counts of criteria b, c, d."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row['input'], row['arm'])].append(row)
    out = []
    for (input_id, arm), cells in sorted(groups.items()):
        entry: dict[str, Any] = {
            'input': input_id,
            'arm': arm,
            'cells': len(cells),
            'errors': sum(c['status'] == 'error' for c in cells),
            'voids': sum(c['status'] == 'void' for c in cells),
            'fail_b': sum(bool(c['b']['fail']) for c in cells),
            'fail_c': sum(bool(c['c']['fail']) for c in cells),
            'disclosed': sum(bool(c['d']['disclosed']) for c in cells),
            'absence_claim': sum(bool(c['d']['absence_claim']) for c in cells),
        }
        for metric in RECALL_METRICS:
            values = [
                c[metric]['recall']
                for c in cells
                if c.get(metric) and c[metric]['recall'] is not None
            ]
            entry[f'{metric}_recall_mean'] = _mean(values)
        out.append(entry)
    return out


def build_metrics(
    *,
    label: str,
    input_set: str,
    verdict: dict[str, Any],
    rows: list[dict[str, Any]],
    provenance: dict[str, Any],
    judge: dict[str, Any] | None,
    cuts: list[str],
) -> dict[str, Any]:
    """The metrics document; raises `ProseLeakError` if any prose-shaped key slipped in."""
    metrics = {
        'schema': SCHEMA,
        'harness_version': HARNESS_VERSION,
        'label': label,
        'input_set': input_set,
        'provenance': provenance,
        'verdict': verdict,
        'aggregates': aggregate(rows),
        'judge': judge,
        'cuts': cuts,
    }
    assert_no_prose(metrics)
    return metrics


def render_markdown(metrics: dict[str, Any]) -> str:
    """The same numbers as a table, generated from `metrics.json` so the two cannot disagree."""
    verdict = metrics['verdict']
    lines = [
        f'# Summary-merge quality gate: {metrics["label"]}',
        '',
        f'**Verdict: {verdict["verdict"]}** ({", ".join(verdict.get("reasons") or ["no findings"])})',
        '',
        f'Input set `{metrics["input_set"]}`. Model `{metrics["provenance"].get("model")}`. '
        f'Margin `{verdict["params"]["margin"]}`'
        + (
            f', effective `{verdict["noise_floor"]["effective_margin"]:.4f}`'
            if 'noise_floor' in verdict
            else ''
        )
        + '.',
        '',
        '| input | arm | cells | err | void | carry | gold dec | gold act | fail b | fail c | disclosed | absence |',
        '|---|---|---|---|---|---|---|---|---|---|---|---|',
    ]

    def fmt(v: float | None) -> str:
        return '-' if v is None else f'{v:.3f}'

    for a in metrics['aggregates']:
        lines.append(
            f'| {a["input"]} | {a["arm"]} | {a["cells"]} | {a["errors"]} | {a["voids"]} '
            f'| {fmt(a["carry_recall_mean"])} | {fmt(a["gold_decisions_recall_mean"])} '
            f'| {fmt(a["gold_actions_recall_mean"])} | {a["fail_b"]} | {a["fail_c"]} '
            f'| {a["disclosed"]} | {a["absence_claim"]} |'
        )
    for cut in metrics['cuts']:
        lines += ['', f'Cut: {cut}']
    lines += [
        '',
        'Recall is a lexical floor (ami_recall): it ranks arms, it is not an accuracy figure.',
        '',
    ]
    return '\n'.join(lines)


def write_metrics(metrics: dict[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path, md_path = directory / 'metrics.json', directory / 'metrics.md'
    json_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    md_path.write_text(render_markdown(metrics), encoding='utf-8')
    return json_path, md_path
