#!/usr/bin/env python3
"""Collate per-level benchmark metrics into master tables for the whitepaper.

Reads every ``docs/engine-benchmark-results/<label>/metrics.json`` produced by
``run_benchmark.py`` (old runs without metrics.json are ignored, so stale data
never pollutes the output) and emits:

  - master_results.csv  — one row per (level, batch) with all metrics
  - summary.md          — markdown tables grouped by phase, ready for the
                          whitepaper / docs/BENCHMARK_RESULTS.md

Usage:
    backend/venv/bin/python scripts/collate_benchmark.py [--results-dir DIR]
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DIR = REPO / 'docs' / 'engine-benchmark-results'

# (csv key, markdown header) for the columns we surface in summary tables.
# 'speedup_vs_conc1' is computed here (RTF at conc N / RTF at conc 1 within a phase),
# NOT the per-batch speedup_vs_single from the driver, which is meaningless when each
# level is a single-batch run.
COLUMNS = [
    ('conc', 'Conc'),
    ('files_completed', 'Files'),
    ('throughput_audio_hrs_per_wall_hr', 'RTF (h/h)'),
    ('speedup_vs_conc1', 'Speedup vs c1'),
    ('vram_peak_mb', 'VRAM peak (MB)'),
    ('gpu_util_pct_avg', 'GPU util %'),
    ('cpu_pct_avg', 'CPU avg %'),
    ('ram_mb_avg', 'RAM avg (MB)'),
    ('gpu_transcribe_p50_s', 'GPU p50 (s)'),
    ('gpu_transcribe_p95_s', 'GPU p95 (s)'),
    ('preprocess_p95_s', 'Preproc p95 (s)'),
]

MASTER_FIELDS = [
    'phase',
    'label',
    'conc',
    'batch_size',
    'files_completed',
    'files_errored',
    'throughput_audio_hrs_per_wall_hr',
    'speedup_vs_conc1',
    'batch_wall_s',
    'avg_file_wall_s',
    'avg_gpu_s',
    'gpu_transcribe_p50_s',
    'gpu_transcribe_p95_s',
    'preprocess_p50_s',
    'preprocess_p95_s',
    'vram_peak_mb',
    'vram_avg_mb',
    'gpu_util_pct_avg',
    'cpu_pct_peak',
    'cpu_pct_avg',
    'ram_mb_peak',
    'ram_mb_avg',
]


def parse_label(label: str, batch: dict) -> tuple[str, int]:
    """Derive (phase, concurrency) from a run label like 'a6000_solo_conc8'."""
    if '_conc' in label:
        phase, _, c = label.partition('_conc')
        try:
            return phase, int(c)
        except ValueError:
            return phase, batch.get('batch_size', 0)
    return label, batch.get('batch_size', 0)


def load_rows(results_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for mj in sorted(results_dir.glob('*/metrics.json')):
        data = json.loads(mj.read_text())
        label = data.get('run_label') or mj.parent.name
        for batch in data.get('batches', []):
            phase, conc = parse_label(label, batch)
            row = {'phase': phase, 'label': label, 'conc': conc}
            row.update(batch)
            rows.append(row)
    _add_speedup(rows)
    return rows


def _add_speedup(rows: list[dict]) -> None:
    """Per phase, speedup_vs_conc1 = RTF(conc N) / RTF(conc 1). Blank if no conc-1 baseline.

    This is the meaningful concurrency speedup — computed across levels, since each
    level is a single-batch run (the driver's per-batch speedup is not usable here).
    """
    baselines: dict[str, float] = {}
    for r in rows:
        if r.get('conc') == 1 and r.get('throughput_audio_hrs_per_wall_hr'):
            baselines.setdefault(r['phase'], float(r['throughput_audio_hrs_per_wall_hr']))
    for r in rows:
        base = baselines.get(r['phase'])
        rtf = r.get('throughput_audio_hrs_per_wall_hr')
        r['speedup_vs_conc1'] = round(float(rtf) / base, 2) if base and rtf else ''


def write_master_csv(rows: list[dict], out: Path) -> None:
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=MASTER_FIELDS, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f'Master CSV: {out}  ({len(rows)} rows)')


def md_table(rows: list[dict]) -> list[str]:
    headers = [h for _, h in COLUMNS]
    lines = ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join('---' for _ in headers) + ' |']
    for r in sorted(rows, key=lambda x: (x.get('conc') or 0)):
        cells = []
        for key, _ in COLUMNS:
            v = r.get(key, '')
            cells.append('' if v is None else str(v))
        lines.append('| ' + ' | '.join(cells) + ' |')
    return lines


def write_summary_md(rows: list[dict], out: Path) -> None:
    by_phase: dict[str, list[dict]] = {}
    for r in rows:
        by_phase.setdefault(r['phase'], []).append(r)

    order = ['a6000_solo', 'ti_solo', 'dual_gpu_scale', 'gpu_split', 'duration_curve']
    phases = [p for p in order if p in by_phase] + [p for p in by_phase if p not in order]

    md = [
        '# Engine Optimization Benchmark — Collated Results',
        '',
        'Generated from per-level `metrics.json`. RTF = audio-hours processed per '
        'wall-clock hour (steady-state throughput over the corpus).',
        '',
    ]
    for phase in phases:
        md.append(f'## {phase}')
        md.append('')
        md += md_table(by_phase[phase])
        md.append('')
    out.write_text('\n'.join(md))
    print(f'Summary MD: {out}')
    print('\n' + '\n'.join(md))


def main() -> None:
    ap = argparse.ArgumentParser(description='Collate benchmark metrics')
    ap.add_argument('--results-dir', default=str(DEFAULT_DIR))
    args = ap.parse_args()
    results_dir = Path(args.results_dir)

    rows = load_rows(results_dir)
    if not rows:
        print(f'No metrics.json found under {results_dir} — run a benchmark first.')
        return
    write_master_csv(rows, results_dir / 'master_results.csv')
    write_summary_md(rows, results_dir / 'summary.md')


if __name__ == '__main__':
    main()
