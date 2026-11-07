"""The second half of the pipeline: `replay-merge`, `judge`, `export-calibration`, `report`.

Everything here reads a finished run directory. Nothing here needs the GPU: replay-merge talks to
the same model the run used, `judge` to the judge, and `report` to nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tests.eval.harness.significance import paired_bootstrap_ci
from tests.eval.harness.summary_judge import (
    calibration_report,
    export_calibration,
    judge_cell,
    judge_policy,
    llm_service_judge,
    sentence_id,
    truncation_candidates,
)
from tests.eval.harness.summary_scoring import CellScore, score_cell
from tests.eval.harness.summary_verdict import decide

from summary_bench.cli_support import load_run, merged_text_of
from summary_bench.config import ExitCode, HarnessError
from summary_bench.planner import LOCK_NAME
from summary_bench.replay import MERGE_ARMS, replay_merges
from summary_bench.report import build_metrics, write_metrics

REPLAY_INPUTS = ('I1', 'I2', 'I3')


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows), encoding='utf-8')


# ---------------------------------------------------------------------------- replay-merge


def cmd_replay_merge(args: Namespace) -> int:
    """Re-merge each recorded `old` run's slices under the old and the new merge rules.

    The slice inputs are identical, so this isolates the merge rules from slice-level variance.
    It is reported beside the gate and never used as the gate: the shipped change alters both
    rule sets together.
    """
    from app.core.default_prompts import UNIVERSAL_CONTENT_ANALYZER_PROMPT
    from summary_bench.cli import llm_config_factory, resolve_arms
    from summary_bench.service import build_service_class, default_prompt, inject_arm

    run = load_run(args)
    arms = resolve_arms(_with_flags(args, no_controls=True))
    cls, make = build_service_class(), llm_config_factory(args)
    sources = {
        cell_id: record
        for cell_id, record in sorted(run.records.items())
        if record['arm'] == 'old' and record['input'] in REPLAY_INPUTS and record['status'] == 'ok'
    }

    def do_merge(cell_id: str, name: str) -> dict[str, Any]:
        record = sources[cell_id]
        entry = run.lock['inputs'][record['input']]
        render = run.rendered[record['input']]
        service = cls(make(entry['max_tokens']), arm=arms[name], seed=record['seed'])
        merged = service._combine_sections(
            record['slices'],
            render.speaker_data,
            UNIVERSAL_CONTENT_ANALYZER_PROMPT,
            len(record['slices']),
            'English',
            '',
        )
        score = score_cell(
            cell_id=f'{cell_id}>{name}',
            input_id=record['input'],
            arm=name,
            run=record['run'],
            status='ok',
            void_reason=None,
            merged=merged,
            slice_outputs=record['slices'],
            gold=run.gold[record['input']],
            tail_index=entry.get('tail_index'),
        )
        return {
            'source': cell_id,
            'input': record['input'],
            'run': record['run'],
            'merge_arm': name,
            'carry_recall': score.carry.recall,
            'carry': score.carry.as_row(),
        }

    @contextlib.contextmanager
    def arm_scope(name: str) -> Iterator[None]:
        with inject_arm(arms[name]), default_prompt(UNIVERSAL_CONTENT_ANALYZER_PROMPT):
            yield

    out = replay_merges(
        [(c, n) for c in sources for n in MERGE_ARMS],
        do_merge,
        arm_scope,
        run.dir / 'replay.jsonl',
        concurrency=getattr(args, 'concurrency', 1),
    )
    by_source: dict[str, dict[str, float | None]] = {}
    for row in out:
        by_source.setdefault(row['source'], {})[row['merge_arm']] = row['carry_recall']
    deltas = [
        v['new'] - v['old']
        for v in by_source.values()
        if v.get('old') is not None and v.get('new') is not None
    ]
    summary: dict[str, Any] = {'pairs': len(deltas), 'diagnostic_only': True}
    if deltas:
        ci = paired_bootstrap_ci(deltas)
        summary.update(delta_mean=ci.delta_mean, ci_low=ci.ci_low, ci_high=ci.ci_high)
    (run.dir / 'replay_summary.json').write_text(
        json.dumps(summary, indent=2, sort_keys=True) + '\n'
    )
    print(f'replayed {len(out)} merge calls over {len(by_source)} recorded old runs: {summary}')
    return ExitCode.OK


def _with_flags(args: Namespace, **flags: Any) -> Namespace:
    merged = vars(args).copy()
    merged.update(flags)
    return Namespace(**merged)


# ---------------------------------------------------------------------------- judge


def _judge_fn(args: Namespace) -> tuple[Any, dict[str, Any]]:
    from app.core.enums import LLMProvider
    from app.services.llm_types import LLMConfig

    if args.judge == 'haiku':
        key = os.environ.get(args.api_key_env)
        if not key:
            raise HarnessError(
                f'--judge haiku needs ${args.api_key_env} in the environment', ExitCode.PRECONDITION
            )
        config = LLMConfig(
            provider=LLMProvider.ANTHROPIC, model=args.judge_model, api_key=key, max_tokens=8192
        )
    else:
        if not args.judge_base_url:
            raise HarnessError('--judge local needs --judge-base-url', ExitCode.MISUSE)
        config = LLMConfig(
            provider=LLMProvider(args.judge_provider),
            model=args.judge_model,
            base_url=args.judge_base_url,
            max_tokens=8192,
        )
    return llm_service_judge(config)


def _missed_items(score: CellScore) -> list[tuple[str, str]]:
    """Lexically missed items, keyed by where they were missed, for the paraphrase judge."""
    items = [(f'carry:{o.key}', o.text) for o in score.carry.missed]
    for tag, breakdown in (('gd', score.gold_decisions), ('ga', score.gold_actions)):
        if breakdown:
            items += [(f'{tag}:{o.key}', o.text) for o in breakdown.missed]
    return items


def _judged_recall(score: CellScore, conveyed: set[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for name, tag, breakdown in (
        ('carry', 'carry', score.carry),
        ('gold_decisions', 'gd', score.gold_decisions),
        ('gold_actions', 'ga', score.gold_actions),
    ):
        if breakdown is None or not breakdown.total:
            out[name] = None
            continue
        rescued = sum(1 for o in breakdown.missed if f'{tag}:{o.key}' in conveyed)
        out[name] = (breakdown.recalled + rescued) / breakdown.total
    return out


def cmd_judge(args: Namespace) -> int:
    """Judge every ok cell, blind to arm, input and run. Writes `judge.jsonl` (no corpus text)."""
    if args.judge == 'none':
        print('judge disabled: the lexical floor stays the gate metric')
        return ExitCode.OK
    run = load_run(args)
    judge, provenance = _judge_fn(args)
    rows: list[dict[str, Any]] = []
    for record, score in run.scored:
        if record['status'] != 'ok':
            continue
        merged_text = merged_text_of(record)
        verdict = judge_cell(
            judge,
            summary_text=merged_text,
            missed_items=_missed_items(score),
            slice_says_truncated=score.slice_says_truncated,
            failure_input=record['input'] == 'I4',
            seed=record['seed'],
            nothing_decided_hits=score.nothing_decided_hits,
            empty_decisions=score.empty_decisions,
        )
        judged = {
            'b_fail': verdict['b_fail'],
            'c_fail': verdict['c_fail'],
            'recall': _judged_recall(score, set(verdict['recall_conveyed'])),
            **(
                {'disclosed': verdict['disclosed'], 'absence_claim': verdict['absence_claim']}
                if 'disclosed' in verdict
                else {}
            ),
        }
        rows.append(
            {
                'cell_id': record['cell_id'],
                'judged': judged,
                'degraded': verdict['degraded'],
                'recall_labels': verdict['recall_labels'],
                'b_labels': verdict['b_labels'],
                'b_prefilter_missed': verdict['b_prefilter_missed'],
            }
        )
    _write_jsonl(run.dir / 'judge.jsonl', rows)
    (run.dir / 'judge_provenance.json').write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + '\n'
    )
    degraded = sum(1 for r in rows if r['degraded'])
    print(f'judged {len(rows)} cells ({degraded} with a degraded call) with {provenance}')
    return ExitCode.OK


# ---------------------------------------------------------------------------- calibration


def cmd_export_calibration(args: Namespace) -> int:
    """Write the blind labelling set (`calibration/items.jsonl`) and its key (kept by the lead)."""
    run = load_run(args)
    pool: list[dict[str, Any]] = []
    for record, score in run.scored:
        if record['status'] != 'ok':
            continue
        summary = merged_text_of(record)
        for key, text in _missed_items(score):
            pool.append(
                {
                    'kind': 'recall',
                    'cell_id': record['cell_id'],
                    'item_id': key,
                    'text': text,
                    'summary': summary,
                }
            )
        hits, sample = truncation_candidates(summary, seed=record['seed'])
        for sentence in [*hits, *sample]:
            pool.append(
                {
                    'kind': 'b',
                    'cell_id': record['cell_id'],
                    'item_id': sentence_id(sentence),
                    'text': sentence,
                    'summary': summary,
                }
            )
        if record['input'] == 'I4':
            pool.append(
                {
                    'kind': 'd',
                    'cell_id': record['cell_id'],
                    'item_id': '-',
                    'text': '',
                    'summary': summary,
                }
            )
    items, key = export_calibration(pool, recall_pairs=args.recall_pairs)
    out = run.dir / 'calibration'
    out.mkdir(exist_ok=True)
    _write_jsonl(out / 'items.jsonl', items)
    (out / 'key.json').write_text(json.dumps(key, indent=2, sort_keys=True) + '\n')
    print(
        f'{len(items)} items for hand labelling in {out / "items.jsonl"}; key (keep it away from the labeller) in key.json'
    )
    return ExitCode.OK


def _judge_labels_for(key: dict[str, Any], judge_rows: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The judge's label for every blind id: yes/no (recall), claim/no (b), D1A0-style (d)."""
    labels: dict[str, str] = {}
    for blind, entry in key.items():
        row = judge_rows.get(entry['cell_id'])
        if row is None:
            continue
        if entry['kind'] == 'recall':
            labels[blind] = 'yes' if row['recall_labels'].get(entry['item_id']) else 'no'
        elif entry['kind'] == 'b':
            labels[blind] = 'claim' if row['b_labels'].get(entry['item_id']) else 'no'
        else:
            j = row['judged']
            labels[blind] = (
                f'D{int(j.get("disclosed", False))}A{int(j.get("absence_claim", False))}'
            )
    return labels


def _calibrate(run_dir: Path, labels_path: str | None) -> dict[str, Any] | None:
    key_path = run_dir / 'calibration' / 'key.json'
    if not labels_path or not key_path.exists():
        return None
    key = json.loads(key_path.read_text())
    human = json.loads(Path(labels_path).read_text())
    judge_rows = {r['cell_id']: r for r in _read_jsonl(run_dir / 'judge.jsonl')}
    judge_labels = _judge_labels_for(key, judge_rows)
    return calibration_report({b: judge_labels[b] for b in human}, human)


# ---------------------------------------------------------------------------- report


def cmd_report(args: Namespace) -> int:
    run = load_run(args)
    rows = [score.scores_row() for _, score in run.scored]
    judge_rows = {r['cell_id']: r for r in _read_jsonl(run.dir / 'judge.jsonl')}
    calibration = _calibrate(run.dir, args.labels)
    policy = judge_policy(calibration['cohens_kappa']) if calibration else 'unused'
    for row in rows:
        if row['cell_id'] in judge_rows:
            row['judged'] = judge_rows[row['cell_id']]['judged']
    _write_jsonl(run.dir / 'scores.jsonl', rows)
    matrix = {
        tuple(k.split('|', 1)): n
        for k, n in json.loads((run.dir / 'matrix.json').read_text()).items()
    }
    verdict = decide(
        rows,
        matrix=matrix,
        margin=args.margin,
        use_judge=policy == 'verdict',
        require_controls=run.meta.get('controls', True) and not args.no_controls,
    )
    judge_block = None
    if judge_rows or calibration:
        provenance_path = run.dir / 'judge_provenance.json'
        judge_block = {
            'policy': policy,
            'calibration': calibration,
            'provenance': json.loads(provenance_path.read_text())
            if provenance_path.exists()
            else None,
            'cells_judged': len(judge_rows),
            'prefilter_misses': sum(r['b_prefilter_missed'] for r in judge_rows.values()),
        }
    provenance = {
        'model': run.meta.get('model'),
        'provider': run.meta.get('provider'),
        'vllm_image': run.meta.get('vllm_image'),
        'llm_models': run.meta.get('llm_models'),
        'app_git_sha': run.meta.get('app_git_sha'),
        'arms': run.meta['arms'],
        'lock_app_git_sha': run.lock.get('app_git_sha'),
        'seed_base': 20261105,
        'tier': 'A',
        'sources': 'AMI, ICSI (CC BY 4.0), QMSum (MIT)',
    }
    metrics = build_metrics(
        label=args.label or run.dir.name,
        input_set=run.meta['input_set'],
        verdict=verdict,
        rows=rows,
        provenance=provenance,
        judge=judge_block,
        cuts=run.meta.get('cuts', []),
    )
    json_path, _ = write_metrics(metrics, run.dir)
    print(f'verdict: {verdict["verdict"]} {verdict.get("reasons")}  ->  {json_path}')
    if args.publish:
        target = Path(args.publish)
        target.mkdir(parents=True, exist_ok=True)
        for name in ('metrics.json', 'metrics.md', LOCK_NAME):
            shutil.copy2(run.dir / name, target / name)
        print(f'published (prose-free) to {target}')
    if args.fail_on_error and any(r['status'] == 'error' for r in rows):
        print('refused: at least one cell errored', file=sys.stderr)
        return ExitCode.FAILED
    return ExitCode.OK if verdict['verdict'] != 'VOID' else ExitCode.FAILED
