"""The run matrix: cell order, one cell's summary, and the append-only records file.

Every cell yields a record. A cell whose LLM call errors, times out or returns garbage is recorded
`status: error` and scores zero on every metric; it is not retried silently. `--retry-errors`
re-runs only those, and the file keeps both attempts (the latest wins when records are loaded).
Records hold corpus-derived prose, so they live outside the repo and are never committed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from summary_bench.arms import Arm
from summary_bench.config import SEED_BASE, ExitCode, HarnessError, ensure_app_environment
from summary_bench.inputs import InputSpec, RenderedInput
from summary_bench.service import (
    Injection,
    Recorder,
    build_service_class,
    default_prompt,
    inject_arm,
)

RECORDS_NAME = 'records.jsonl'


@dataclass(frozen=True)
class CellId:
    """One (input, arm, run) summary to produce."""

    input_id: str
    arm: str
    run: int

    @property
    def key(self) -> str:
        return f'{self.input_id}|{self.arm}|{self.run}'


def plan_cells(
    specs: tuple[InputSpec, ...], arms: list[str], matrix: dict[tuple[str, str], int]
) -> list[CellId]:
    """Cells in execution order: blocks by run index, arm order a seeded permutation per block.

    Seeding the permutation by the run index means ``old`` does not always go first and warm the
    prefix cache for ``new``. Inside an arm, inputs run in their fixed order.
    """
    cells: list[CellId] = []
    ordered_arms = sorted(arms)
    blocks = max((n for n in matrix.values()), default=0)
    for k in range(blocks):
        permutation = np.random.default_rng(k).permutation(len(ordered_arms))
        for arm in (ordered_arms[int(i)] for i in permutation):
            cells.extend(
                CellId(spec.input_id, arm, k)
                for spec in specs
                if k < matrix.get((spec.input_id, arm), 0)
            )
    return cells


def build_matrix(
    specs: tuple[InputSpec, ...],
    arms: list[str],
    runs: int,
    *,
    aa_inputs: tuple[str, ...] | None = None,
) -> dict[tuple[str, str], int]:
    """The plan 4.5 matrix restricted to the arms and inputs actually requested."""
    from tests.eval.harness.summary_verdict import gate_matrix

    wanted = {s.input_id for s in specs}
    matrix = {(i, a): n for (i, a), n in gate_matrix(runs).items() if i in wanted and a in arms}
    # Inputs the full matrix does not know (dev, smoke ids) run every requested arm for `runs`.
    for spec in specs:
        if not any(i == spec.input_id for i, _ in matrix):
            matrix.update({(spec.input_id, a): runs for a in arms if a != 'sabotage'})
    if aa_inputs is not None:
        matrix = {k: v for k, v in matrix.items() if k[1] != 'old-aa' or k[0] in aa_inputs}
    return matrix


@dataclass
class RunContext:
    """Everything a cell needs; built once per run."""

    specs: dict[str, InputSpec]
    lock: dict[str, Any]
    rendered: dict[str, RenderedInput]
    arms: dict[str, Arm]
    llm_config_factory: Callable[[int], Any]
    prompt: str
    transport: Callable[..., dict[str, Any]] | None = None
    service_cls: type | None = field(default=None, repr=False)


def _entry_key(spec: InputSpec) -> str:
    return spec.shares_with or spec.input_id


def _merge_failed(merged: dict[str, Any] | None) -> bool:
    """The app swallows a merge failure into a "partial results" summary with `metadata.error`."""
    return merged is None or bool((merged.get('metadata') or {}).get('error'))


def _failure_of(
    merged: dict[str, Any] | None, recorder: Recorder, injection: Injection | None
) -> str | None:
    """Why a cell produced no usable summary, as a class-like name (never message text)."""
    if _merge_failed(merged):
        return 'MergeFailed'
    sections = recorder.merge_inputs[-1] if recorder.merge_inputs else []
    injected = injection.slice_index if injection else None
    # A slice that failed for real (timeout, 5xx, garbage), as opposed to the one the harness
    # failed on purpose, makes the cell incomparable to its pair: it is an error cell.
    if any(sec.get('_error') for i, sec in enumerate(sections, start=1) if i != injected):
        return 'SliceFailed'
    return None


def _void_reason(
    recorder: Recorder, entry: dict[str, Any], injection: Injection | None
) -> str | None:
    if recorder.applied_failures:
        return 'arm_not_applied'
    if not recorder.merge_inputs:
        return 'no_merge_call'
    sections = recorder.merge_inputs[-1]
    if len(sections) != len(entry['chunks']):
        return 'slice_count_mismatch'
    if injection and not sections[injection.slice_index - 1].get('_error'):
        return 'failure_not_injected'
    return None


def run_cell(ctx: RunContext, cell: CellId) -> dict[str, Any]:
    """Produce one cell's record: the slices the merge received, the merged summary, the calls."""
    ensure_app_environment()
    spec = ctx.specs[cell.input_id]
    entry = ctx.lock['inputs'][cell.input_id]
    render = ctx.rendered[_entry_key(spec)]
    arm = ctx.arms[cell.arm]
    injection = Injection(spec.failure_mode, entry['failing_slice']) if spec.failure_mode else None
    cls = ctx.service_cls or build_service_class()
    service = cls(
        ctx.llm_config_factory(entry['max_tokens']),
        arm=arm,
        seed=SEED_BASE + cell.run,
        injection=injection,
        transport=ctx.transport,
    )
    started = time.monotonic()
    # No broad `except` here, deliberately. The app already turns every LLM failure into data:
    # a failed slice becomes an `_error` placeholder and a failed merge becomes a "partial
    # results" summary with `metadata.error`. Both are classified below. Anything that still
    # raises is a bug in the harness or the app and must stop the run, not be recorded as a
    # model failure (a fail-open handler would score it as a zero and carry on).
    with inject_arm(arm), default_prompt(ctx.prompt):
        merged = service.generate_summary(render.transcript, render.speaker_data)
    error = _failure_of(merged, service.recorder, injection)
    recorder = service.recorder
    void = None if error else _void_reason(recorder, entry, injection)
    status = 'error' if error else ('void' if void else 'ok')
    merges = [c for c in recorder.calls if c.phase == 'merge']
    return {
        'cell_id': cell.key,
        'input': cell.input_id,
        'arm': cell.arm,
        'run': cell.run,
        'seed': SEED_BASE + cell.run,
        'status': status,
        'void_reason': void,
        'error': error,
        'arm_hashes': arm.as_record(),
        'slices': recorder.merge_inputs[-1] if recorder.merge_inputs else [],
        'merged': None if error else merged,
        'calls': [asdict(c) for c in recorder.calls],
        'applied': {
            'checked': sum(1 for c in recorder.calls if c.applied is not None),
            'failed': recorder.applied_failures,
        },
        'merge_attempts': len(merges),  # more than 1: the truncation retry fired
        'failing_slice': injection.slice_index if injection else None,
        'mode': injection.mode if injection else None,
        'wall_s': round(time.monotonic() - started, 2),
    }


def load_records(path: Path) -> dict[str, dict[str, Any]]:
    """Latest attempt per cell id from a records file (empty when it does not exist)."""
    latest: dict[str, dict[str, Any]] = {}
    attempts: dict[str, int] = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                row = json.loads(line)
                attempts[row['cell_id']] = attempts.get(row['cell_id'], 0) + 1
                row['attempt'] = attempts[row['cell_id']]
                latest[row['cell_id']] = row
    return latest


def pending_cells(
    cells: list[CellId], done: dict[str, dict[str, Any]], *, resume: bool, retry_errors: bool
) -> list[CellId]:
    """Cells still to run. Without ``resume`` an existing records file is refused, not appended."""
    if done and not (resume or retry_errors):
        raise HarnessError(
            f'{len(done)} records already exist in this run directory; pass --resume to continue '
            'it or --retry-errors to re-run only the failed cells',
            ExitCode.MISUSE,
        )
    redo = {'error'} if retry_errors else set()
    return [c for c in cells if c.key not in done or done[c.key]['status'] in redo]


def run_cells(
    ctx: RunContext, cells: list[CellId], path: Path, on_record: Callable[[dict[str, Any]], None]
) -> Iterator[dict[str, Any]]:
    """Run ``cells`` strictly one at a time, appending each record the moment it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for cell in cells:
        record = run_cell(ctx, cell)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, sort_keys=True) + '\n')
        on_record(record)
        yield record
