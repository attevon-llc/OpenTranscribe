"""Print a run summary and compare two stored variant reports."""

from __future__ import annotations

import json

from pyannote_direct.config import RESULTS_DIR, TEST_FILES


def print_summary(report: dict) -> None:
    """Formatted summary (statistical when runs > 1)."""
    print()
    variant = report['variant']
    runs = report.get('runs', 1)
    print(f'{"=" * 110}')
    title = f'  BENCHMARK RESULTS — {variant.upper()} PyAnnote'
    if report.get('tag'):
        title += f' [tag={report["tag"]}]'
    if runs > 1:
        title += f'  ({runs}-run statistical)'
    print(title)
    gpu_name = (report.get('gpu_info') or {}).get('gpu_name', 'N/A')
    print(f'  Device: {report["device_str"]} | GPU: {gpu_name}')
    print(f'  Model: {report.get("model_id")} | Load: {report["pipeline_load_time_s"]}s')
    if report.get('pyannote_fork_sha'):
        print(
            f'  Fork SHA: {report["pyannote_fork_sha"][:12]}  ({report.get("pyannote_fork_path")})'
        )
    print(f'{"=" * 110}')
    if runs > 1:
        print(
            f'  {"File":<16} {"Wall mean":>11} {"stdev":>8} {"cv":>6} '
            f'{"Peak VRAM":>10} {"Steady":>8} {"Spkrs":>6} {"Segs":>6} {"Reliable":>9}'
        )
    else:
        print(
            f'  {"File":<16} {"Wall":>8} {"Peak VRAM":>10} {"Steady":>8} '
            f'{"Spkrs":>7} {"Segs":>8} {"Stages"}'
        )
    print(f'  {"-" * 104}')
    for f in report['files']:
        errors = f.get('errors', 0)
        agg = f.get('aggregate', {}) or {}
        wall_agg = agg.get('wall_seconds', {})
        peak_agg = agg.get('overall_peak_vram_mb', {})
        steady_agg = agg.get('steady_state_vram_mb', {})
        reliable = agg.get('reliable', True)
        if runs > 1:
            if not wall_agg:
                print(f'  {f["label"]:<16} ERROR (all runs failed)')
                continue
            print(
                f'  {f["label"]:<16} '
                f'{wall_agg.get("mean", 0):>10.2f}s '
                f'{wall_agg.get("stdev", 0):>7.2f}s '
                f'{wall_agg.get("cv", 0):>5.1%} '
                f'{peak_agg.get("mean", 0):>8.0f}MB '
                f'{steady_agg.get("mean", 0):>6.0f}MB '
                f'{f.get("speaker_count", 0):>6} '
                f'{f.get("segment_count", 0):>6} '
                f'{"yes" if reliable else "NO":>9}'
            )
        else:
            stages_str = ', '.join(f'{k}={v:.1f}s' for k, v in f.get('stage_durations', {}).items())
            # Use first-run peak/steady if no aggregate
            first_peak = (
                peak_agg.get('mean') if peak_agg else f['runs'][0].get('overall_peak_vram_mb', 0)
            )
            first_steady = (
                steady_agg.get('mean')
                if steady_agg
                else f['runs'][0].get('steady_state_vram_mb', 0)
            )
            if errors:
                print(f'  {f["label"]:<16} ERROR in {errors}/{runs} runs')
                continue
            print(
                f'  {f["label"]:<16} {f.get("total_seconds", 0):>7.1f}s '
                f'{int(first_peak or 0):>8}MB '
                f'{int(first_steady or 0):>6}MB '
                f'{f.get("speaker_count", 0):>7} {f.get("segment_count", 0):>8}   '
                f'{stages_str}'
            )
    print(f'  {"-" * 104}')
    s = report['summary']
    print(
        f'  Total first-run time: {s["total_time_s"]}s across {s["total_files"]} files, {s["errors"]} errors'
    )
    print(f'{"=" * 110}')


def compare_results(variant_a: str, variant_b: str) -> None:
    """Compare two benchmark result sets (legacy single-run compare)."""
    file_a = RESULTS_DIR / f'benchmark_{variant_a}_latest.json'
    file_b = RESULTS_DIR / f'benchmark_{variant_b}_latest.json'

    if not file_a.exists():
        print(f"No results for '{variant_a}' — run benchmark first")
        return
    if not file_b.exists():
        print(f"No results for '{variant_b}' — run benchmark first")
        return

    with open(file_a) as f:
        report_a = json.load(f)
    with open(file_b) as f:
        report_b = json.load(f)

    results_a = {r['label']: r for r in report_a['files']}
    results_b = {r['label']: r for r in report_b['files']}

    print()
    print(f'{"=" * 110}')
    print(f'  COMPARISON: {variant_a.upper()} vs {variant_b.upper()}')
    print(f'  {variant_a}: {report_a["timestamp"]} | {variant_b}: {report_b["timestamp"]}')
    print(f'{"=" * 110}')
    print(
        f'  {"File":<16} '
        f'{"Time A":>8} {"Time B":>8} {"Speedup":>8} '
        f'{"Spkrs A":>8} {"Spkrs B":>8} '
        f'{"Segs A":>8} {"Segs B":>8} {"Seg Diff":>9}'
    )
    print(f'  {"-" * 104}')

    total_a = 0.0
    total_b = 0.0
    for label in TEST_FILES:
        ra = results_a.get(label)
        rb = results_b.get(label)
        if not ra or not rb:
            continue
        if 'error' in ra or 'error' in rb:
            print(f'  {label:<16} ERROR in one or both')
            continue

        ta = ra['total_seconds']
        tb = rb['total_seconds']
        total_a += ta
        total_b += tb
        speedup = ta / tb if tb > 0 else 0
        seg_diff = rb['segment_count'] - ra['segment_count']
        seg_pct = (seg_diff / ra['segment_count'] * 100) if ra['segment_count'] > 0 else 0

        print(
            f'  {label:<16} '
            f'{ta:>7.1f}s {tb:>7.1f}s {speedup:>7.2f}x '
            f'{ra["speaker_count"]:>8} {rb["speaker_count"]:>8} '
            f'{ra["segment_count"]:>8} {rb["segment_count"]:>8} '
            f'{seg_diff:>+8} ({seg_pct:>+.1f}%)'
        )

    print(f'  {"-" * 104}')
    overall_speedup = total_a / total_b if total_b > 0 else 0
    print(f'  {"TOTAL":<16} {total_a:>7.1f}s {total_b:>7.1f}s {overall_speedup:>7.2f}x')
    print(f'{"=" * 110}')

    print('\n  OUTPUT ACCURACY (segment timing differences):')
    print(f'  {"-" * 90}')
    for label in TEST_FILES:
        ra = results_a.get(label)
        rb = results_b.get(label)
        if not ra or not rb or 'error' in ra or 'error' in rb:
            continue
        segs_a = ra.get('all_segments', [])
        segs_b = rb.get('all_segments', [])
        if len(segs_a) != len(segs_b):
            print(f'  [{label}] Segment count mismatch: {len(segs_a)} vs {len(segs_b)}')
        else:
            max_diff = 0.0
            total_diff = 0.0
            speaker_mismatches = 0
            for sa, sb in zip(segs_a, segs_b, strict=False):
                d = abs(sa['start'] - sb['start']) + abs(sa['end'] - sb['end'])
                max_diff = max(max_diff, d)
                total_diff += d
                if sa['speaker'] != sb['speaker']:
                    speaker_mismatches += 1
            avg_diff = total_diff / len(segs_a) if segs_a else 0
            speech_a = ra.get('total_speech_seconds', 0)
            speech_b = rb.get('total_speech_seconds', 0)
            print(
                f'  [{label}] {len(segs_a)} segments | '
                f'timing: avg_diff={avg_diff:.4f}s max_diff={max_diff:.4f}s | '
                f'speaker_mismatches={speaker_mismatches} | '
                f'speech: {speech_a:.1f}s vs {speech_b:.1f}s'
            )
    print(f'  {"-" * 90}')

    print('\n  STAGE BREAKDOWN:')
    print(f'  {"-" * 90}')
    for label in TEST_FILES:
        ra = results_a.get(label)
        rb = results_b.get(label)
        if not ra or not rb or 'error' in ra or 'error' in rb:
            continue
        stages_a = ra.get('stage_durations', {})
        stages_b = rb.get('stage_durations', {})
        all_stages = sorted(set(list(stages_a.keys()) + list(stages_b.keys())))
        print(f'  [{label}]')
        for stage in all_stages:
            sa = stages_a.get(stage, 0)
            sb = stages_b.get(stage, 0)
            sp = sa / sb if sb > 0 else 0
            print(f'    {stage:<30} {sa:>7.2f}s -> {sb:>7.2f}s  ({sp:.2f}x)')
    print(f'  {"-" * 90}')
