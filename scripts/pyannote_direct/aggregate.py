"""Statistical aggregation across repeated runs."""

from __future__ import annotations

import statistics
from typing import Any

_NUMERIC_METRICS = (
    'wall_seconds',
    'overall_peak_vram_mb',
    'overall_peak_over_baseline_mb',
    'steady_state_vram_mb',
    'baseline_alloc_mb',
    'speaker_count',
    'segment_count',
    'total_speech_seconds',
)


def _aggregate_scalar(values: list[float]) -> dict[str, float]:
    if not values:
        return {'n': 0}
    if len(values) == 1:
        return {
            'n': 1,
            'mean': values[0],
            'stdev': 0.0,
            'min': values[0],
            'max': values[0],
            'cv': 0.0,
        }
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values)
    return {
        'n': len(values),
        'mean': round(mean, 4),
        'stdev': round(stdev, 4),
        'min': round(min(values), 4),
        'max': round(max(values), 4),
        'cv': round(stdev / mean, 4) if mean else 0.0,
    }


def aggregate_runs(runs: list[dict]) -> dict:
    """Produce per-metric mean/stdev/min/max/cv and per-stage aggregates."""
    if not runs:
        return {'runs': []}

    agg: dict[str, Any] = {}
    for metric in _NUMERIC_METRICS:
        agg[metric] = _aggregate_scalar([r[metric] for r in runs if metric in r])

    # Per-stage aggregates
    stage_names: set[str] = set()
    for r in runs:
        stage_names.update(r.get('stage_durations', {}).keys())

    stage_agg: dict[str, dict[str, Any]] = {}
    for stage in stage_names:
        durs = [r['stage_durations'].get(stage, 0) for r in runs if 'stage_durations' in r]
        cum = [
            r.get('stage_cumulative_peak_vram_mb', {}).get(stage, 0)
            for r in runs
            if 'stage_cumulative_peak_vram_mb' in r
        ]
        delta = [
            r.get('stage_vram_delta_mb', {}).get(stage, 0)
            for r in runs
            if 'stage_vram_delta_mb' in r
        ]
        stage_agg[stage] = {
            'duration_s': _aggregate_scalar(durs),
            'cumulative_peak_vram_mb': _aggregate_scalar(cum),
            'vram_delta_mb': _aggregate_scalar(delta),
        }
    agg['stages'] = stage_agg

    # Reliability flag — Phase A rule: cv > 10% on wall_seconds means "rerun"
    wall = agg.get('wall_seconds', {})
    agg['reliable'] = bool(wall and wall.get('cv', 1.0) <= 0.10)

    return agg
