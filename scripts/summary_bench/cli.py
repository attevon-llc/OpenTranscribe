"""Dispatch and the first half of the pipeline: `plan-slices`, `run`, `project`."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

from summary_bench.arms import Arm, load_arm, sabotage_arm
from summary_bench.config import (
    ExitCode,
    HarnessError,
    ensure_app_environment,
    logger,
)
from summary_bench.inputs import INPUT_SETS, CorpusPaths, InputSpec
from summary_bench.parser import build_parser
from summary_bench.planner import (
    app_git_sha,
    build_lock,
    read_lock,
    verify_lock,
    write_lock,
)
from summary_bench.runner import (
    RECORDS_NAME,
    RunContext,
    build_matrix,
    load_records,
    pending_cells,
    plan_cells,
    run_cells,
)

SMOKE_SET = 'smoke'


def specs_for(args: Namespace) -> tuple[InputSpec, ...]:
    return INPUT_SETS[args.inputs]


def lock_dir_for(work_dir: str | Path, input_set: str) -> Path:
    """One lock per input set; the smoke input (I1) is planned with, and locked by, the gate set."""
    return Path(work_dir) / 'lock' / ('gate' if input_set == SMOKE_SET else input_set)


def lock_path(args: Namespace) -> Path:
    if getattr(args, 'lock', None):
        return Path(args.lock).expanduser()
    return lock_dir_for(args.work_dir, args.inputs)


def run_dir_for(args: Namespace) -> Path:
    if args.run_dir:
        return Path(args.run_dir).expanduser()
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    return Path(args.work_dir) / 'runs' / f'{stamp}-{app_git_sha()}'


def llm_config_factory(args: Namespace) -> Any:
    """``max_tokens -> LLMConfig`` for the model under test. A key is read by NAME, never shown."""
    from app.core.enums import LLMProvider
    from app.services.llm_types import LLMConfig

    api_key = os.environ.get(args.api_key_env) if args.provider == 'anthropic' else None
    if args.provider == 'anthropic' and not api_key:
        raise HarnessError(
            f'--provider anthropic needs ${args.api_key_env} in the environment',
            ExitCode.PRECONDITION,
        )
    base_url = None if args.provider == 'anthropic' else args.llm_base_url

    def make(max_tokens: int) -> Any:
        return LLMConfig(
            provider=LLMProvider(args.provider),
            model=args.model,
            api_key=api_key,
            base_url=base_url,
            max_tokens=max_tokens,
        )

    return make


def resolve_arms(args: Namespace) -> dict[str, Arm]:
    """The requested arms plus the controls: ``old-aa`` (old again) and ``sabotage``."""
    arms = {a.name: a for a in (load_arm(spec) for spec in args.arm)}
    if 'old' not in arms or 'new' not in arms:
        raise HarnessError('--arm old=... and --arm new=... are both required')
    if not args.no_controls:
        arms.setdefault('old-aa', arms['old'].renamed('old-aa'))
        arms.setdefault('sabotage', sabotage_arm())
    return arms


def cmd_plan_slices(args: Namespace) -> int:
    paths = CorpusPaths.under(Path(args.data_dir))
    specs = specs_for(args)
    lock = build_lock(specs, paths)
    out = Path(args.out).expanduser() if args.out else lock_dir_for(args.work_dir, args.inputs)
    path = write_lock(lock, out)
    for input_id, entry in lock['inputs'].items():
        sizes = [c['chars'] for c in entry['chunks']]
        tail = (
            f', tail {sizes[-1]} chars (slice {entry["tail_index"]})' if entry['tail_index'] else ''
        )
        failing = f', failing slice {entry["failing_slice"]}' if entry['failing_slice'] else ''
        print(
            f'{input_id}: max_tokens={entry["max_tokens"]} slices={len(sizes)} chars={sizes}'
            f'{tail}{failing} gold={entry["gold"]["layers"]} '
            f'located/slice={entry["gold"]["located_per_slice"]} unlocated={entry["gold"]["unlocated"]}'
        )
    print(f'lock written: {path}')
    return ExitCode.OK


def _llm_models(args: Namespace) -> Any:
    """The server's own `/v1/models` answer, for provenance. Best effort; never fatal."""
    if args.provider == 'anthropic':
        return None
    try:
        import requests

        return requests.get(f'{args.llm_base_url.rstrip("/")}/models', timeout=10).json()
    except (OSError, ValueError):
        return {'unreachable': True}


def cmd_run(args: Namespace) -> int:
    ensure_app_environment()
    specs = specs_for(args)
    arms = resolve_arms(args)
    aa_inputs = tuple(args.aa_inputs.split(',')) if args.aa_inputs else None
    matrix = build_matrix(specs, sorted(arms), args.runs, aa_inputs=aa_inputs)
    cells = plan_cells(specs, sorted(arms), matrix)
    if args.dry_run:
        print(f'{len(cells)} cells, strictly sequential, in this order:')
        for index, cell in enumerate(cells, start=1):
            print(f'  {index:3d}. {cell.key}')
        return ExitCode.OK

    paths = CorpusPaths.under(Path(args.data_dir))
    lock = read_lock(lock_path(args))
    rendered = verify_lock(lock, specs, paths)
    from app.core.default_prompts import UNIVERSAL_CONTENT_ANALYZER_PROMPT

    run_dir = run_dir_for(args)
    records_path = run_dir / RECORDS_NAME
    todo = pending_cells(
        cells, load_records(records_path), resume=args.resume, retry_errors=args.retry_errors
    )
    ctx = RunContext(
        specs={s.input_id: s for s in specs},
        lock=lock,
        rendered=rendered,
        arms=arms,
        llm_config_factory=llm_config_factory(args),
        prompt=UNIVERSAL_CONTENT_ANALYZER_PROMPT,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    write_lock(lock, run_dir)
    (run_dir / 'matrix.json').write_text(
        json.dumps({f'{i}|{a}': n for (i, a), n in sorted(matrix.items())}, indent=2) + '\n'
    )
    meta = {
        'input_set': args.inputs,
        'controls': not args.no_controls,
        'arms': {name: arm.as_record() for name, arm in sorted(arms.items())},
        'model': args.model,
        'provider': args.provider,
        'vllm_image': args.vllm_image,
        'llm_models': _llm_models(args),
        'app_git_sha': app_git_sha(),
        'cells_planned': len(cells),
    }
    (run_dir / 'meta.json').write_text(json.dumps(meta, indent=2, sort_keys=True) + '\n')
    print(f'run dir: {run_dir}  ({len(todo)} of {len(cells)} cells to run)')

    started = time.monotonic()
    done: list[dict[str, Any]] = []

    def progress(record: dict[str, Any]) -> None:
        print(
            f'[{len(done) + 1}/{len(todo)}] {record["cell_id"]} {record["status"]}'
            f'{" (" + record["void_reason"] + ")" if record["void_reason"] else ""}'
            f'{" " + record["error"] if record["error"] else ""} {record["wall_s"]}s',
            flush=True,
        )

    for record in run_cells(ctx, todo, records_path, progress):
        done.append(record)
    _write_runinfo(run_dir, done, time.monotonic() - started)
    return ExitCode.OK


def _write_runinfo(run_dir: Path, records: list[dict[str, Any]], wall_s: float) -> None:
    """Append this session's timing and applied-check counts (a resumed run has several)."""
    calls = [c for r in records for c in r['calls']]
    session = {
        'wall_clock_s': round(wall_s, 1),
        'cells_run': len(records),
        'seconds_per_summary': round(wall_s / len(records), 2) if records else None,
        'llm_calls': len(calls),
        'applied_checked': sum(r['applied']['checked'] for r in records),
        'applied_failed': sum(r['applied']['failed'] for r in records),
        'injected_calls': sum(1 for c in calls if c['injected']),
        'prompt_tokens': sum(c['prompt_tokens'] or 0 for c in calls),
        'completion_tokens': sum(c['completion_tokens'] or 0 for c in calls),
        'truncation_retries': sum(max(0, r['merge_attempts'] - 1) for r in records),
        'errors': sum(r['status'] == 'error' for r in records),
        'voids': sum(r['status'] == 'void' for r in records),
    }
    path = run_dir / 'runinfo.json'
    sessions = json.loads(path.read_text())['sessions'] if path.exists() else []
    path.write_text(json.dumps({'sessions': [*sessions, session]}, indent=2, sort_keys=True) + '\n')


def cmd_project(args: Namespace) -> int:
    """Refuse (exit 4, "not measured") when the full matrix would not fit the budget."""
    from tests.eval.harness.summary_verdict import gate_matrix

    info = json.loads((Path(args.run_dir) / 'runinfo.json').read_text())['sessions'][-1]
    per_summary = info.get('seconds_per_summary')
    if not per_summary:
        raise HarnessError('the smoke run recorded no timing', ExitCode.NOT_MEASURED)
    cells = sum(gate_matrix(args.runs).values())
    replay_calls = 6 * args.runs  # I1-I3 old runs, each merged under two rule sets
    hours = (per_summary * cells + per_summary * 0.4 * replay_calls) / 3600
    print(
        f'{per_summary:.1f} s/summary x {cells} cells (+{replay_calls} merge-only calls) '
        f'= {hours:.2f} h projected; budget {args.budget_hours:.1f} h'
    )
    if hours > args.budget_hours:
        print('projection exceeds the budget: cut old-aa to I1 and I3, then lower --runs to 3')
        return ExitCode.NOT_MEASURED
    return ExitCode.OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, 'verbose', False) else logging.WARNING,
        format='%(levelname)s %(message)s',
    )
    # The app's chunker logs its decisions at INFO for every candidate budget the planner tries.
    # Quieted here, at the entry point: set inside the library, it leaked into the backend test
    # session that imports it (tests/eval/) and hid other tests' DEBUG/INFO records.
    logging.getLogger('app.services.llm_service').setLevel(logging.WARNING)
    logging.getLogger('app.services.llm_chunking').setLevel(logging.WARNING)
    ensure_app_environment()
    from summary_bench import post

    commands = {
        'plan-slices': cmd_plan_slices,
        'run': cmd_run,
        'project': cmd_project,
        'replay-merge': post.cmd_replay_merge,
        'judge': post.cmd_judge,
        'export-calibration': post.cmd_export_calibration,
        'report': post.cmd_report,
    }
    try:
        return commands[args.command](args)
    except HarnessError as exc:
        logger.error('%s', exc)
        print(f'refused: {exc}', file=sys.stderr)
        return exc.code
