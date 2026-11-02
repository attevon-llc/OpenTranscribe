"""Locate and read a committed baseline's metrics.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rag_bench.config import DEFAULT_BASELINE_ROOT


def resolve_baseline_metrics_path(baseline: str) -> Path:
    """A baseline name under ``tests/eval/baselines/``, or an explicit path.

    Args:
        baseline: e.g. ``miracl-es-english``, a baseline directory, or a direct
            path to a ``metrics.json``.

    Returns:
        The resolved ``metrics.json`` path.

    Raises:
        SystemExit: nothing on disk matches ``baseline``.
    """
    candidate = Path(baseline)
    if candidate.is_file():
        return candidate
    if candidate.is_dir():
        return candidate / 'metrics.json'
    by_name = DEFAULT_BASELINE_ROOT / baseline
    if by_name.is_dir():
        return by_name / 'metrics.json'
    raise SystemExit(
        f'--compare-only: {baseline!r} is not a metrics.json path, a baseline directory, '
        f'or a name under {DEFAULT_BASELINE_ROOT}'
    )


def load_baseline_metrics(baseline: str) -> dict[str, Any]:
    path = resolve_baseline_metrics_path(baseline)
    if not path.is_file():
        raise SystemExit(f'--compare-only: no metrics.json at {path}')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise SystemExit(f'--compare-only: {path} is not a JSON object at the top level')
    return payload
