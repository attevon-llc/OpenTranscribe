#!/usr/bin/env python3
"""Grade probe answers with the label judge, and calibrate it against human labels (#518).

Three subcommands over one probe run's ``results.json`` (``probe_chat_rag.py --out``):

``sheet``
    Emit a grading sheet (JSONL: item, category, question, reference, answer,
    ``human_label: ""``) for a grader to fill in. The sheet is BLIND — it carries no
    judge output, so the grader cannot anchor on the judge's opinion.

``judge``
    Run the label judge (``tests.eval.harness.answer_judge.run_judge``) over every
    record, against an OpenAI-compatible endpoint. ⚠️ The judge model must NOT be the
    model that produced the answers — a judge grading its own generations measures
    self-preference, not correctness. Pass ``--answering-model`` so the script can
    refuse that configuration instead of trusting the operator to remember.

``kappa``
    Join a filled sheet with a judgements file on ``item`` and print
    ``agreement_report`` — Cohen's Kappa, the Landis & Koch band, the raw-vs-Kappa
    overstatement, and the confusion matrix. Judgements flagged ``degraded`` (the
    judge's reply was unparseable) are excluded from the Kappa and counted separately:
    a Kappa computed partly over fallback labels measures the fallback regex.

⚠️ **Licence boundary:** sheets and judgements quote question/reference/answer prose
(QMSum/AMI reference text) and judge ``why`` sentences — they must stay under the
gitignored ``.rag-403/`` tree, never be committed. The ``kappa`` report is
numbers-and-labels only and IS safe to commit or paste into an issue.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

VALID_LABELS = ('FULL', 'PARTIAL', 'NONE', 'REFUSED')


#: Stands in as the "reference" for a negative-control record, which has none by
#: construction. The judge prompt's REFUSED rule reads the ANSWER, not the
#: reference, so grading still works — and without these records the calibration
#: set would carry no REFUSED examples at all, leaving that label's agreement
#: unmeasured.
NEGATIVE_CONTROL_REFERENCE = (
    '(negative control: the topic/speaker asked about is absent from the corpus — '
    'the correct answer DECLINES rather than inventing content)'
)


def _load_results(path: Path) -> list[dict[str, Any]]:
    records = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(records, list) or not records:
        raise SystemExit(f'{path}: expected a non-empty JSON list of probe records')
    kept: list[dict[str, Any]] = []
    skipped: list[str] = []
    for record in records:
        if record.get('reference_answer'):
            kept.append(record)
        elif record.get('category') == 'negative_control' or record.get('expect_refusal'):
            kept.append({**record, 'reference_answer': NEGATIVE_CONTROL_REFERENCE})
        else:
            skipped.append(record.get('label', '?'))
    if skipped:
        print(
            f'⚠️  {len(skipped)} non-control record(s) have no reference_answer and are '
            f'skipped: {skipped[:5]}{"..." if len(skipped) > 5 else ""}',
            file=sys.stderr,
        )
    return kept


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _guard_rag403(path: Path) -> None:
    """Refuse to write prose-bearing artifacts outside the gitignored tree."""
    if '.rag-403' not in path.resolve().parts:
        raise SystemExit(
            f'{path}: sheets/judgements carry QMSum/AMI reference prose and must live '
            'under .rag-403/ (gitignored). Only the kappa report is committable.'
        )


def cmd_sheet(args: argparse.Namespace) -> int:
    records = _load_results(args.results)
    _guard_rag403(args.out)
    rows = [
        {
            'item': r['label'],
            'category': r['category'],
            'question': r['question'],
            'reference': r['reference_answer'],
            'answer': r['app_answer'] or '',
            'human_label': '',
        }
        for r in records
    ]
    _write_jsonl(args.out, rows)
    print(f'sheet: {len(rows)} items -> {args.out}')
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    from tests.eval.harness.answer_judge import JudgeEndpoint, run_judge

    if args.answering_model.strip().lower() == args.judge_model.strip().lower():
        raise SystemExit(
            f'judge model == answering model ({args.judge_model!r}): a judge grading '
            'its own generations measures self-preference, not correctness (#518). '
            'Point --judge-base-url/--judge-model at the OTHER local server.'
        )
    records = _load_results(args.results)
    _guard_rag403(args.out)
    endpoint = JudgeEndpoint(
        base_url=args.judge_base_url, model=args.judge_model, api_key=args.judge_api_key
    )
    from openai import OpenAI

    client = OpenAI(
        base_url=endpoint.base_url, api_key=endpoint.api_key, timeout=endpoint.timeout_s
    )
    rows = []
    for index, record in enumerate(records, 1):
        judgement = run_judge(
            endpoint,
            question=record['question'],
            reference=record['reference_answer'],
            answer=record['app_answer'] or '',
            client=client,
        )
        rows.append(
            {
                'item': record['label'],
                'category': record['category'],
                'judge_label': judgement.label,
                'covered': judgement.covered,
                'total': judgement.total,
                'why': judgement.why,
                'degraded': judgement.degraded,
            }
        )
        print(f'[{index}/{len(records)}] {record["label"]}: {judgement.label}', file=sys.stderr)
    _write_jsonl(args.out, rows)
    provenance = {'judge': endpoint.as_provenance(), 'n': len(rows)}
    print(json.dumps(provenance))
    return 0


def cmd_kappa(args: argparse.Namespace) -> int:
    from tests.eval.harness.answer_judge import agreement_report

    labels = {row['item']: row for row in _read_jsonl(args.labels)}
    judgements = {row['item']: row for row in _read_jsonl(args.judgements)}
    unlabeled = [i for i, row in labels.items() if not row.get('human_label')]
    bad = [
        i
        for i, row in labels.items()
        if row.get('human_label') and row['human_label'] not in VALID_LABELS
    ]
    if bad:
        raise SystemExit(f'invalid human_label values (want one of {VALID_LABELS}): {bad[:10]}')
    shared = sorted(set(labels) & set(judgements))
    degraded = [i for i in shared if judgements[i].get('degraded')]
    usable = [i for i in shared if labels[i].get('human_label') and i not in set(degraded)]
    if not usable:
        raise SystemExit('no items are both human-labelled and non-degraded — nothing to compare')
    report = agreement_report(
        [judgements[i]['judge_label'] for i in usable],
        [labels[i]['human_label'] for i in usable],
    )
    report['items_compared'] = len(usable)
    report['items_degraded_excluded'] = len(degraded)
    report['items_unlabelled'] = len(unlabeled)
    report['disagreements'] = [
        {'item': i, 'judge': judgements[i]['judge_label'], 'human': labels[i]['human_label']}
        for i in usable
        if judgements[i]['judge_label'] != labels[i]['human_label']
    ]
    output = json.dumps(report, indent=2)
    print(output)
    if args.report_out:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(output + '\n', encoding='utf-8')
        print(f'report -> {args.report_out}', file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    sheet = sub.add_parser('sheet', help='emit a blind grading sheet')
    sheet.add_argument('--results', required=True, type=Path)
    sheet.add_argument('--out', required=True, type=Path)
    sheet.set_defaults(func=cmd_sheet)

    judge = sub.add_parser('judge', help='run the label judge over a probe run')
    judge.add_argument('--results', required=True, type=Path)
    judge.add_argument('--out', required=True, type=Path)
    judge.add_argument('--judge-base-url', required=True)
    judge.add_argument('--judge-model', required=True)
    judge.add_argument('--judge-api-key', default='not-needed')
    judge.add_argument(
        '--answering-model',
        required=True,
        help='the model that produced the answers being graded — refused as judge',
    )
    judge.set_defaults(func=cmd_judge)

    kappa = sub.add_parser('kappa', help='agreement report: judge vs human labels')
    kappa.add_argument('--labels', required=True, type=Path, help='filled grading sheet')
    kappa.add_argument('--judgements', required=True, type=Path)
    kappa.add_argument('--report-out', type=Path, default=None)
    kappa.set_defaults(func=cmd_kappa)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    sys.exit(main())
