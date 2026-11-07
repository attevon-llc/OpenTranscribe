"""Argument parser for `benchmark_summary.py` (one subcommand per stage of the plan)."""

from __future__ import annotations

import argparse
import os

from summary_bench.config import (
    DEFAULT_BASELINE_ROOT,
    DEFAULT_DATA_DIR,
    DEFAULT_MODEL,
    DEFAULT_WORK_DIR,
)
from summary_bench.inputs import INPUT_SETS

#: Haiku 4.5 on purpose, for the judge and the remote output-shape leg: the app sends an assistant
#: prefill (`{`) and temperature 0.1 on Anthropic calls, which the newer Claude models reject.
DEFAULT_REMOTE_MODEL = 'claude-haiku-4-5'


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument('--data-dir', default=str(DEFAULT_DATA_DIR), help='benchmark corpora root')
    p.add_argument('--work-dir', default=str(DEFAULT_WORK_DIR), help='where lock/arms/runs live')
    p.add_argument('--inputs', choices=sorted(INPUT_SETS), default='gate')
    p.add_argument('-v', '--verbose', action='store_true')


def _llm(p: argparse.ArgumentParser) -> None:
    port = os.environ.get('LLM_TEST_PORT', '5195')
    p.add_argument('--provider', default='vllm', help='vllm | anthropic | ollama | custom')
    p.add_argument('--model', default=DEFAULT_MODEL)
    p.add_argument('--llm-base-url', default=f'http://localhost:{port}/v1')
    p.add_argument(
        '--api-key-env',
        default='ANTHROPIC_API_KEY',
        help='NAME of the environment variable holding the key (used by name, never printed)',
    )
    p.add_argument('--vllm-image', default=None, help='recorded in the run provenance')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='benchmark_summary.py',
        description='Measure the multi-slice summary scope and merge rules on a real model.',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    plan = sub.add_parser('plan-slices', help='choose and lock the slice layout (no LLM, no GPU)')
    _common(plan)
    plan.add_argument('--out', default=None, help='directory for inputs.lock.json')

    run = sub.add_parser('run', help='produce the matrix of summaries')
    _common(run)
    _llm(run)
    run.add_argument('--arm', action='append', default=[], metavar='NAME=@module|PATH.json')
    run.add_argument('--runs', type=int, default=5)
    run.add_argument('--lock', default=None, help='inputs.lock.json (default: work-dir/lock)')
    run.add_argument('--run-dir', default=None)
    run.add_argument(
        '--concurrency',
        type=int,
        default=1,
        help='summaries in flight at once within one arm and run block (default 1: sequential, '
        'comparable with older runs; 8-12 suits the test vLLM, whose --max-num-seqs is 16). '
        'Per-cell wall_s and call latency are not comparable when above 1',
    )
    run.add_argument('--resume', action='store_true')
    run.add_argument('--retry-errors', action='store_true')
    run.add_argument('--no-controls', action='store_true', help='skip old-aa and sabotage (smoke)')
    run.add_argument('--aa-inputs', default=None, help='comma list; limit old-aa to these inputs')
    run.add_argument('--dry-run', action='store_true', help='print the cells in order and stop')

    project = sub.add_parser('project', help='project the full matrix wall clock from a smoke run')
    project.add_argument('--run-dir', required=True)
    project.add_argument('--runs', type=int, default=5)
    project.add_argument('--budget-hours', type=float, default=4.0)

    replay = sub.add_parser('replay-merge', help='re-merge recorded old slices under both rules')
    _common(replay)
    _llm(replay)
    replay.add_argument('--arm', action='append', default=[], metavar='NAME=@module|PATH.json')
    replay.add_argument('--run-dir', required=True)
    replay.add_argument('--lock', default=None)
    replay.add_argument(
        '--concurrency',
        type=int,
        default=1,
        help='merge calls in flight at once within one merge arm (default 1). Rows are '
        'checkpointed to replay.jsonl as they finish and a rerun skips them',
    )

    judge = sub.add_parser('judge', help='LLM-as-judge pass over a finished run')
    _common(judge)
    judge.add_argument('--run-dir', required=True)
    judge.add_argument('--lock', default=None)
    judge.add_argument('--judge', choices=['haiku', 'local', 'none'], default='haiku')
    judge.add_argument('--judge-model', default=DEFAULT_REMOTE_MODEL)
    judge.add_argument('--judge-base-url', default=None, help='for --judge local')
    judge.add_argument('--judge-provider', default='vllm', help='for --judge local')
    judge.add_argument('--api-key-env', default='ANTHROPIC_API_KEY')

    cal = sub.add_parser('export-calibration', help='write the blind hand-labelling set')
    _common(cal)
    cal.add_argument('--run-dir', required=True)
    cal.add_argument('--recall-pairs', type=int, default=40)

    rep = sub.add_parser('report', help='score, decide and write metrics.json / metrics.md')
    _common(rep)
    rep.add_argument('--run-dir', required=True)
    rep.add_argument('--lock', default=None)
    rep.add_argument('--label', default=None)
    rep.add_argument('--margin', type=float, default=0.05)
    rep.add_argument(
        '--labels', default=None, help='human calibration labels (JSON, blind id -> label)'
    )
    rep.add_argument('--no-controls', action='store_true', help='smoke: waive the control arms')
    rep.add_argument(
        '--fail-on-error', action='store_true', help='exit 1 if any cell errored (wiring checks)'
    )
    rep.add_argument(
        '--publish',
        default=None,
        help=f'copy lock + metrics to DIR (e.g. {DEFAULT_BASELINE_ROOT}/<date>-<sha>)',
    )
    return parser
