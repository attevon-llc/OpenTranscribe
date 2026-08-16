#!/usr/bin/env python3
"""Score the query router as a classifier (#403 Stage 4, unit 1).

    python3 scripts/benchmark_router.py                  # both corpora, prints a table
    python3 scripts/benchmark_router.py --corpus synthetic --out /tmp/route

Deliberately **not** a mode of ``benchmark_rag.py``. Routing is a pure function
of the query string: it needs no OpenSearch, no Postgres, no settle check and no
LLM, so folding it into a script whose first act is to refuse an unsettled index
would make a measurement that cannot lie about the corpus depend on one that can.

It reads more queries than the retrieval benchmark can score, on purpose. The
retrieval harness only sees queries whose gold set is fully injected (75 of the
synthetic tier's 1,000); routing depends on none of that, so the whole labelled
population is used and the count is reported.

What the numbers mean, and which of them are independent evidence:
``docs-site/docs/developer-guide/rag-evaluation.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.scripts.corpus_injection.env import bootstrap  # noqa: E402

logger = logging.getLogger('benchmark_router')

DEFAULT_MANIFEST_ROOT = REPO_ROOT / '.rag-403' / 'injections'
DEFAULT_BASELINE_ROOT = BACKEND / 'tests' / 'eval' / 'baselines'
DEFAULT_DATA_DIR = Path('/mnt/nas/opentranscribe-benchmarks')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', action='append', default=None, help='qmsum | synthetic')
    parser.add_argument('--manifest-root', default=str(DEFAULT_MANIFEST_ROOT))
    parser.add_argument('--data-dir', default=str(DEFAULT_DATA_DIR))
    parser.add_argument('--control-name', default='stage4-router')
    parser.add_argument('--out', default=None, help='Output dir [tests/eval/baselines/<name>]')
    parser.add_argument(
        '--list-misroutes',
        type=int,
        default=0,
        help='Print up to N misrouted queries with the signals that fired. The point of '
        'the measurement is to act on them, and a rate with no examples cannot be acted on.',
    )
    parser.add_argument('-v', '--verbose', action='store_true')
    return parser


def _qmsum_cases(manifest_root: Path):
    """QMSum's 1,576 human queries, labelled by the harness's own surface rule."""
    from tests.eval.harness import corpora as corpora_mod
    from tests.eval.harness.routing import EXPECTED_INTENT_BY_CLASS, SURFACE_RULE, RoutingCase

    manifest_dir = manifest_root / 'qmsum'
    if not (manifest_dir / 'manifest.json').is_file():
        return []
    corpus = corpora_mod.load_manifest(manifest_dir)
    return [
        RoutingCase(
            query_id=query.query_id,
            text=query.text,
            expected=EXPECTED_INTENT_BY_CLASS[query.query_class],
            query_class=query.query_class,
            corpus='qmsum',
            label_provenance=SURFACE_RULE,
        )
        for query in corpora_mod.load_qmsum_queries(corpus)
        if query.query_class in EXPECTED_INTENT_BY_CLASS
    ]


def _synthetic_dates(manifest_root: Path) -> dict[str, str]:
    """``corpus file_uuid -> ISO date``, from the injection manifest.

    The generator's own meeting shards carry the date too, but they are 233 MB of
    turn text to recover one field per meeting. The manifest is local, small, and
    covers whatever was actually injected — a query whose gold files are not in it
    simply makes no claim about the temporal slot rather than being scored against
    a date nobody has.
    """
    path = manifest_root / 'synthetic' / 'files.jsonl'
    if not path.is_file():
        return {}
    dates: dict[str, str] = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        extra = json.loads(line).get('extra') or {}
        corpus_uuid, date = extra.get('corpus_file_uuid'), extra.get('date')
        if corpus_uuid and date:
            dates[str(corpus_uuid)] = str(date)
    return dates


def _synthetic_cases(manifest_root: Path, data_dir: Path):
    """The generator's whole labelled query population, labels true by construction."""
    from tests.eval.harness import corpora as corpora_mod
    from tests.eval.harness.routing import BY_CONSTRUCTION, EXPECTED_INTENT_BY_CLASS, RoutingCase

    manifest_dir = manifest_root / 'synthetic'
    source = data_dir / 'synthetic'
    if (manifest_dir / 'manifest.json').is_file():
        corpus = corpora_mod.load_manifest(manifest_dir)
        if (corpus.root / 'queries.jsonl').is_file():
            source = corpus.root
    path = source / 'queries.jsonl'
    if not path.is_file():
        return []

    dates = _synthetic_dates(manifest_root)
    cases = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        query_class = str(record.get('query_class') or '')
        if query_class not in EXPECTED_INTENT_BY_CLASS:
            continue
        # The expected date comes from the GOLD FILES' own dates, never from the
        # query text — parsing the text would be scoring the router's extractor
        # against itself.
        #
        # Only rules that ARE temporal make a claim. The first run keyed this on
        # "the gold files share a month", which is true by coincidence for plenty
        # of single-file lookups that assert nothing about a date: the denominator
        # inflated to 158 and the recovery rate read 7% for a router that was in
        # fact recovering the slot whenever one was asked for.
        rule = str(record.get('rule') or '')
        expected_temporal = None
        if 'temporal' in rule:
            gold_dates = {
                dates[uuid][:7] for uuid in (record.get('gold_files') or []) if uuid in dates
            }
            if len(gold_dates) == 1:
                year, month = next(iter(gold_dates)).split('-')
                expected_temporal = (int(year), int(month))
        cases.append(
            RoutingCase(
                query_id=f'synthetic:{record["query_id"]}',
                text=str(record['text']),
                expected=EXPECTED_INTENT_BY_CLASS[query_class],
                query_class=query_class,
                corpus='synthetic',
                label_provenance=BY_CONSTRUCTION,
                rule=rule,
                expected_temporal=expected_temporal,
            )
        )
    return cases


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s %(message)s'
    )
    bootstrap(REPO_ROOT)

    from tests.eval.harness.routing import (
        build_routing_report,
        evaluate_routing,
        render_routing_table,
    )

    manifest_root = Path(args.manifest_root)
    data_dir = Path(args.data_dir)
    keys = args.corpus or ['qmsum', 'synthetic']

    cases = []
    for key in keys:
        loaded = (
            _qmsum_cases(manifest_root)
            if key == 'qmsum'
            else _synthetic_cases(manifest_root, data_dir)
        )
        if not loaded:
            logger.warning('No labelled queries loaded for corpus %r', key)
        cases.extend(loaded)
    if not cases:
        raise SystemExit(
            'No labelled queries found. Both loaders need '
            f'{manifest_root}/<corpus>/manifest.json; the synthetic one also needs the '
            "generator's queries.jsonl under the corpus root or --data-dir."
        )

    result = evaluate_routing(cases)
    report = build_routing_report(cases, result)
    table = render_routing_table(result)

    out_dir = Path(args.out) if args.out else DEFAULT_BASELINE_ROOT / args.control_name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'routing.json').write_text(
        json.dumps({'control_name': args.control_name, **report}, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    (out_dir / 'routing.md').write_text(table, encoding='utf-8')

    print(table)
    leakage = report['lookup_leakage']
    print(
        f'lookup leakage: {leakage["misrouted"]}/{leakage["n"]} '
        f'({leakage["rate"]:.4%}) -> {leakage["to"] or "none"}'
    )
    slot = report['temporal_slot']
    print(f'temporal slot: {slot["recovered"]}/{slot["cases"]} recovered ({slot["rate"]:.4f})')

    if args.list_misroutes:
        by_id = {case.query_id: case for case in cases}
        shown = 0
        print('\nMisroutes:')
        for query_id in sorted(result.predicted):
            case = by_id[query_id]
            predicted = result.predicted[query_id]
            if predicted == case.expected:
                continue
            print(
                f'  [{case.expected} -> {predicted}] {case.rule or case.corpus} '
                f'signals={list(result.signals[query_id])}\n    {case.text[:160]}'
            )
            shown += 1
            if shown >= args.list_misroutes:
                break

    logger.info('Wrote %s', out_dir / 'routing.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
