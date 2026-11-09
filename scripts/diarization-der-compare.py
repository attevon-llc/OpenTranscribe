#!/usr/bin/env python3
"""Compute DER between two sets of RTTM output directories from the benchmark harness.

Complements scripts/diarization-der.py (which is hardwired for Phase A's
docs/diarization-vram-profile/raw/rttm layout). This reads the new
benchmark/results/rttm/<tag>/ layout produced by benchmark-pyannote-direct.py.

Usage:
    # Score Phase-3 RTTM against the fp32 baseline reference
    python scripts/diarization-der-compare.py \\
        --reference benchmark/results/rttm/baseline_a6000_short \\
        --hypothesis benchmark/results/rttm/phase3_gpu_clustering_a6000_short

Output: markdown table (stdout) plus optional JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from pyannote.metrics.diarization import DiarizationErrorRate
from rttm_io import read_rttm


def group_runs(rttm_dir: Path) -> dict[str, list[Path]]:
    """Group RTTM files by base label. Expects names like `<label>_run<N>.rttm`."""
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(rttm_dir.glob('*.rttm')):
        name = p.stem
        label = name.rsplit('_run', 1)[0] if '_run' in name else name
        groups[label].append(p)
    return dict(groups)


def classify(der: float, spk_match: bool) -> str:
    if not spk_match:
        return 'T3'
    if der <= 0.001:
        return 'T1'
    if der <= 0.005:
        return 'T2'
    if der <= 0.01:
        return 'T3'
    return 'T4'


def score(
    ref_dir: Path,
    hyp_dir: Path,
    collar: float = 0.25,
    skip_overlap: bool = False,
) -> list[dict]:
    ref_groups = group_runs(ref_dir)
    hyp_groups = group_runs(hyp_dir)

    metric = DiarizationErrorRate(collar=collar, skip_overlap=skip_overlap)
    results: list[dict] = []

    for label in sorted(set(ref_groups) | set(hyp_groups)):
        ref_paths = ref_groups.get(label, [])
        hyp_paths = hyp_groups.get(label, [])
        if not ref_paths or not hyp_paths:
            results.append(
                {
                    'label': label,
                    'error': 'missing_ref' if not ref_paths else 'missing_hyp',
                }
            )
            continue
        # Use run 0 as the canonical reference; compare each hypothesis run to it
        ref = read_rttm(ref_paths[0])
        ref_spk = len(ref.labels())
        for hyp_path in hyp_paths:
            hyp = read_rttm(hyp_path)
            der = float(metric(ref, hyp))
            hyp_spk = len(hyp.labels())
            seg_ref = sum(1 for _ in ref.itertracks())
            seg_hyp = sum(1 for _ in hyp.itertracks())
            results.append(
                {
                    'label': label,
                    'ref_path': str(ref_paths[0]),
                    'hyp_path': str(hyp_path),
                    'ref_spk': ref_spk,
                    'hyp_spk': hyp_spk,
                    'ref_seg': seg_ref,
                    'hyp_seg': seg_hyp,
                    'der': round(der, 6),
                    'der_pct': round(der * 100.0, 4),
                    'spk_match': hyp_spk == ref_spk,
                    'tier': classify(der, hyp_spk == ref_spk),
                }
            )
    return results


def print_markdown(results: list[dict], ref_dir: Path, hyp_dir: Path) -> None:
    print('# DER Comparison\n')
    print(f'- Reference: `{ref_dir}`')
    print(f'- Hypothesis: `{hyp_dir}`')
    print('- Metric: `DiarizationErrorRate(collar=0.25, skip_overlap=False)`\n')
    print('| file | run | ref_spk | hyp_spk | ref_seg | hyp_seg | DER (%) | spk_match | tier |')
    print('|---|---|---:|---:|---:|---:|---:|:---:|:---:|')
    for r in results:
        if 'error' in r:
            print(f'| {r["label"]} | - | - | - | - | - | - | - | ERR: {r["error"]} |')
            continue
        run = Path(r['hyp_path']).stem.rsplit('_run', 1)[-1]
        print(
            f'| {r["label"]} | {run} | {r["ref_spk"]} | {r["hyp_spk"]} | '
            f'{r["ref_seg"]} | {r["hyp_seg"]} | {r["der_pct"]:.4f} | '
            f'{"✓" if r["spk_match"] else "✗"} | **{r["tier"]}** |'
        )


def main() -> int:
    parser = argparse.ArgumentParser(description='DER comparison between two RTTM dirs')
    parser.add_argument(
        '--reference', required=True, help='Baseline RTTM directory (typically fp32 reference)'
    )
    parser.add_argument('--hypothesis', required=True, help='Candidate RTTM directory to score')
    parser.add_argument('--collar', type=float, default=0.25)
    parser.add_argument(
        '--skip-overlap',
        action='store_true',
        help='Pass skip_overlap=True to DiarizationErrorRate (default False)',
    )
    parser.add_argument(
        '--json-out',
        default=None,
        help='Optional path to dump the raw results JSON',
    )
    args = parser.parse_args()

    ref_dir = Path(args.reference)
    hyp_dir = Path(args.hypothesis)
    if not ref_dir.is_dir():
        print(f'Reference dir missing: {ref_dir}', file=sys.stderr)
        return 1
    if not hyp_dir.is_dir():
        print(f'Hypothesis dir missing: {hyp_dir}', file=sys.stderr)
        return 1

    results = score(ref_dir, hyp_dir, collar=args.collar, skip_overlap=args.skip_overlap)
    print_markdown(results, ref_dir, hyp_dir)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2))

    # Exit code 1 if any hypothesis shows DER > 0.3% OR speaker mismatch
    bad = [r for r in results if 'error' not in r and (r['der'] > 0.003 or not r['spk_match'])]
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
