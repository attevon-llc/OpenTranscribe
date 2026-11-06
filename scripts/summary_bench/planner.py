"""``plan-slices``: render each input, choose its `max_tokens`, write and verify the lock file.

No LLM and no GPU. The lock holds hashes, lengths, counts and gold item ids per slice: no prose,
so it is the one artifact of the run that is committed (plan section 6.4).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from summary_bench.config import HARNESS_VERSION, REPO_ROOT, ExitCode, HarnessError
from summary_bench.gold import PlacedGold, gold_counts, load_gold
from summary_bench.inputs import (
    CorpusPaths,
    InputSpec,
    RenderedInput,
    chunk_transcript,
    failing_slice,
    plan_slices,
    render_input,
    sha256,
    slice_index_for,
    verify_against_lock,
)

LOCK_NAME = 'inputs.lock.json'


def app_git_sha() -> str:
    """Short SHA of the checkout the lock was planned from, or "unknown" outside git."""
    try:
        out = subprocess.run(
            ['git', '-C', str(REPO_ROOT), 'rev-parse', '--short', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return 'unknown'
    return out.stdout.strip()


def _gold_entries(gold: list[PlacedGold], chunks: list[str]) -> list[dict[str, Any]]:
    return [
        {
            'id': g.sentence_id,
            'meeting': g.meeting_id,
            'layer': g.layer,
            'linked': g.linked,
            'slice': None if g.time_s is None else slice_index_for(g.time_s, chunks),
        }
        for g in gold
    ]


def plan_input(spec: InputSpec, paths: CorpusPaths, shared: dict[str, Any]) -> dict[str, Any]:
    """The lock entry for one input; an input that shares another's layout reuses its plan."""
    if spec.shares_with and spec.shares_with in shared:
        base = shared[spec.shares_with]
        entry = {k: v for k, v in base.items() if k != 'failing_slice'}
        entry.update(meetings=list(spec.meetings), kind=spec.kind, failure_mode=spec.failure_mode)
        entry['failing_slice'] = base['_failing_slice']
        entry['_failing_slice'] = base['_failing_slice']
        return entry
    render = render_input(spec, paths)
    gold = load_gold(spec, paths, render.offsets)
    plan = plan_slices(spec, render, [g.time_s for g in gold])
    chunks, _ = chunk_transcript(render.transcript, plan.max_tokens)
    counts = gold_counts(gold, chunks)
    located = {int(k): v for k, v in counts['located_per_slice'].items()}
    fail = failing_slice(located, len(chunks), plan.tail_index)
    return {
        'meetings': list(spec.meetings),
        'kind': spec.kind,
        'failure_mode': spec.failure_mode,
        'max_tokens': plan.max_tokens,
        'response_tokens': plan.response_tokens,
        'transcript_sha256': sha256(render.transcript),
        'transcript_chars': len(render.transcript),
        'offsets': {m: round(o, 3) for m, o in render.offsets.items()},
        'chunks': [
            {'chars': n, 'sha256': h}
            for n, h in zip(plan.chunk_chars, plan.chunk_sha256, strict=True)
        ],
        'tail_index': plan.tail_index,
        'failing_slice': None,
        '_failing_slice': fail,
        'near_misses': plan.near_misses,
        'gold': counts,
        'gold_items': _gold_entries(gold, chunks),
    }


def build_lock(specs: tuple[InputSpec, ...], paths: CorpusPaths) -> dict[str, Any]:
    """Plan every input in ``specs`` and assemble the lock document."""
    planned: dict[str, Any] = {}
    for spec in specs:
        planned[spec.input_id] = plan_input(spec, paths, planned)
    inputs = {}
    for spec in specs:
        entry = dict(planned[spec.input_id])
        fail = entry.pop('_failing_slice')
        entry['failing_slice'] = fail if spec.failure_mode else None
        inputs[spec.input_id] = entry
    return {
        'version': 1,
        'harness_version': HARNESS_VERSION,
        'app_git_sha': app_git_sha(),
        'inputs': inputs,
    }


def write_lock(lock: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOCK_NAME
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return path


def read_lock(path: Path) -> dict[str, Any]:
    if path.is_dir():
        path = path / LOCK_NAME
    try:
        lock: dict[str, Any] = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise HarnessError(
            f'cannot read the slice lock {path}: run plan-slices first', ExitCode.PRECONDITION
        ) from exc
    return lock


def verify_lock(
    lock: dict[str, Any], specs: tuple[InputSpec, ...], paths: CorpusPaths
) -> dict[str, RenderedInput]:
    """Re-render and re-chunk every input; raise (exit 3) when any chunk hash differs."""
    rendered: dict[str, RenderedInput] = {}
    for spec in specs:
        entry = lock['inputs'].get(spec.input_id)
        if entry is None:
            raise HarnessError(
                f'{spec.input_id} is not in the lock; re-run plan-slices', ExitCode.PRECONDITION
            )
        key = spec.shares_with or spec.input_id
        if key not in rendered:
            rendered[key] = render_input(spec, paths)
        problems = verify_against_lock(entry, rendered[key].transcript)
        if problems:
            raise HarnessError(
                f'{spec.input_id}: the chunker no longer reproduces the locked slices '
                f'({"; ".join(problems)}). Re-run plan-slices; arms planned before and after '
                'a chunker change are not comparable.',
                ExitCode.PRECONDITION,
            )
    return rendered
