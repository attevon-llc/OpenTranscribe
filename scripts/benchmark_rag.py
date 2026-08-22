#!/usr/bin/env python3
"""Measure retrieval quality over an injected eval corpus (#403 Stage 1).

One command, under five minutes, byte-identical on two consecutive runs:

    ./opentr.sh bench rag                      # the wrapper, resolves stack ports
    python3 scripts/benchmark_rag.py --corpus qmsum --out <dir>

It drives the **chat** retrieval path (``retrieve_chunks``) over whatever the
corpus injector put on the stack, maps each corpus's gold turn ranges onto the
chunks the production indexer actually produced, and scores the result with
``trec_eval`` via ``pytrec_eval_terrier``.

No LLM is involved at any point (D6). The harness refuses the shared dev stack
for the same reason the injector does.

Methodology, and the four ways a retrieval benchmark lies to you:
``docs-site/docs/developer-guide/rag-evaluation.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / 'backend'
# `backend` first: it is what makes both `app.*` and `tests.eval.harness.*`
# importable from a script that lives outside it.
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

logger = logging.getLogger('benchmark_rag')

DEFAULT_MANIFEST_ROOT = REPO_ROOT / '.rag-403' / 'injections'
DEFAULT_BASELINE_ROOT = BACKEND / 'tests' / 'eval' / 'baselines'
DEFAULT_DATA_DIR = Path('/mnt/nas/opentranscribe-benchmarks')


#: The fusion flags, in one place, because "was this arm explicitly selected?"
#: is answered by whether ANY of them was passed — and a flag added to the
#: parser but forgotten here would silently read as the configured default.
FUSION_FLAGS = (
    'fusion',
    'rank_constant',
    'normalization_technique',
    'combination_technique',
    'combination_weights',
)


def _add_fusion_arguments(parser: argparse.ArgumentParser) -> None:
    """The #363 A/B arm selector.

    Every flag defaults to ``None`` rather than to the shipped value, so
    "measure whatever this deployment is configured for" and "measure RRF at
    k=30" stay distinguishable in the results file. A sweep arm that cannot be
    told apart from the default is how a table of numbers loses its labels.
    """
    group = parser.add_argument_group(
        'fusion (#363)',
        'Hybrid fusion strategy for THIS run. Pass nothing to measure the '
        "deployment's configured default. The pipeline id is derived from these "
        'parameters, so two arms are never aliased onto one pipeline.',
    )
    group.add_argument(
        '--fusion',
        default=None,
        choices=('rrf', 'normalization'),
        help='rrf = score-ranker-processor; normalization = normalization-processor',
    )
    group.add_argument(
        '--rank-constant', type=int, default=None, help='RRF only. #363 asks for 30 and 60.'
    )
    group.add_argument(
        '--normalization-technique',
        default=None,
        choices=('min_max', 'l2', 'z_score'),
        help='normalization only: how each leg is normalised before combining',
    )
    group.add_argument(
        '--combination-technique',
        default=None,
        choices=('arithmetic_mean', 'geometric_mean', 'harmonic_mean'),
        help='normalization only: how the normalised legs are combined',
    )
    group.add_argument(
        '--combination-weights',
        default=None,
        help='normalization only: per-leg weights, e.g. "0.7,0.3" (BM25 leg first). '
        'Encoded into the pipeline id as integer percent; more precision is refused.',
    )


def _build_fusion(args):
    """Resolve the fusion flags into a ``FusionConfig``, or None for the default.

    Unspecified parameters inherit from the *configured* default rather than
    from a second set of literals here — one source of truth for what
    "unspecified" means, and it keeps ``--fusion rrf`` alone meaning "RRF as
    this deployment configures it".

    Args:
        args: Parsed CLI namespace.

    Returns:
        The requested config, or None when no fusion flag was passed.

    Raises:
        SystemExit: If the requested combination is one OpenSearch would reject.
            Refused here rather than at the wire, because a pipeline that was
            never created makes the next search run UNFUSED — a plausible
            number, not an error.
    """
    if all(getattr(args, name) is None for name in FUSION_FLAGS):
        return None

    from app.services.search.fusion import FusionConfig, FusionConfigError, parse_weights

    base = FusionConfig.default()
    try:
        return FusionConfig(
            strategy=args.fusion or base.strategy,
            rank_constant=(
                base.rank_constant if args.rank_constant is None else args.rank_constant
            ),
            normalization_technique=(args.normalization_technique or base.normalization_technique),
            combination_technique=(args.combination_technique or base.combination_technique),
            weights=(
                base.weights
                if args.combination_weights is None
                else parse_weights(args.combination_weights)
            ),
        )
    except FusionConfigError as exc:
        raise SystemExit(f'Unusable --fusion configuration: {exc}') from exc


def _latency_summary(samples: list[float], workers: int) -> dict[str, Any]:
    """Quantiles of the per-query retrieval cost, for the Phase 7 p95 gate.

    Lives in ``runinfo.json``, never in ``metrics.json``: a duration cannot be
    byte-identical across runs, and the results document's determinism is what
    makes an arm-to-arm difference attributable.

    ``concurrency`` is recorded beside the numbers because these are measured
    under the harness's own worker pool, so they are a **comparable cost signal
    between arms**, not a user-facing latency figure. Reading them as the
    latter is the mistake the field is named against.

    Args:
        samples: Per-call durations in milliseconds.
        workers: Concurrent retrieval requests in flight during the run.

    Returns:
        Quantiles plus the concurrency they were taken at.
    """
    if not samples:
        return {'samples': 0}
    ordered = sorted(samples)

    def _q(fraction: float) -> float:
        # Nearest-rank: no interpolation, so the value reported is one that was
        # actually observed.
        index = min(len(ordered) - 1, max(0, int(round(fraction * len(ordered))) - 1))
        return round(ordered[index], 1)

    return {
        'samples': len(ordered),
        'concurrency': workers,
        'p50': _q(0.50),
        'p95': _q(0.95),
        'p99': _q(0.99),
        'max': round(ordered[-1], 1),
        'mean': round(sum(ordered) / len(ordered), 1),
    }


def _build_budget(args) -> dict[str, int]:
    """The 48/12/4 sweep's overrides, omitting anything the caller did not set.

    Omitted rather than defaulted here so the harness's own defaults — which
    are pinned to the shipped ``chat.rag.*`` values — remain the single source
    of what "the production budget" is.

    Args:
        args: Parsed CLI namespace.

    Returns:
        Keyword overrides for ``RetrievalConfig``.
    """
    return {
        name: value
        for name, value in (
            ('final_chunks', args.final_chunks),
            ('max_per_file', args.max_per_file),
            ('rerank_max_pairs', args.rerank_max_pairs),
        )
        if value is not None
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--corpus',
        action='append',
        default=None,
        help='Corpus key to score; repeatable. Default: qmsum',
    )
    parser.add_argument('--user', default='admin@example.com', help='Owner of the injected corpus')
    parser.add_argument('--manifest-root', default=str(DEFAULT_MANIFEST_ROOT))
    parser.add_argument('--data-dir', default=str(DEFAULT_DATA_DIR))
    parser.add_argument('--control-name', default='stage1-baseline')
    parser.add_argument('--out', default=None, help='Output dir [tests/eval/baselines/<name>]')
    parser.add_argument(
        '--stage',
        default='retrieve',
        choices=('retrieve', 'rerank', 'route'),
        help='retrieve = the candidate pool, the committed control. rerank = what reaches '
        'the prompt. route = the production router in the loop, which adds the digest leg on '
        'summarize-routed queries; its ranked list stays chunk-only so nDCG remains '
        'comparable to the control, and the digest tier is reported separately as file '
        'selection.',
    )
    parser.add_argument(
        '--scope',
        default='corpus',
        choices=('corpus', 'gold-files'),
        help='corpus = what chat does; gold-files = ORACLE file selection (upper bound)',
    )
    parser.add_argument(
        '--search-mode', default='hybrid', choices=('hybrid', 'semantic', 'keyword')
    )
    parser.add_argument(
        '--bm25-fields',
        default='default',
        choices=('default', 'no-stem'),
        help='BM25 field set for retrieve_chunks/retrieve_digests (#506, the '
        "no-stemmed-leg arm). 'default' is this module's historical, unboosted "
        "['content', 'content.exact', 'title']. 'no-stem' drops the STEMMED `content` "
        'leg entirely, querying only content.exact/title(/speaker) via '
        "HybridSearchService's own boosted field list (use_exact=True). Recorded in "
        'runinfo.json under retrieval.text_fields_preset.',
    )
    _add_fusion_arguments(parser)
    parser.add_argument('--size', type=int, default=48, help='Candidate pool per query')
    parser.add_argument(
        '--final-chunks',
        type=int,
        default=None,
        help='rerank stage: chunks that reach the prompt. Default: the shipped '
        'chat.rag.final_chunks. One third of the 48/12/4 budget sweep.',
    )
    parser.add_argument(
        '--max-per-file',
        type=int,
        default=None,
        help='rerank stage: ceiling on chunks contributed by any one recording. '
        'Default: the shipped chat.rag.max_chunks_per_file.',
    )
    parser.add_argument(
        '--rerank-max-pairs',
        type=int,
        default=None,
        help='rerank stage: pairs the cross-encoder scores. Default: the shipped '
        'chat.rag.rerank_max_pairs.',
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=4,
        help='Concurrent retrieval requests. Results are keyed by query id, so '
        'this changes wall clock and nothing else.',
    )
    parser.add_argument('--limit-queries', type=int, default=0, help='Score at most N per corpus')
    parser.add_argument('--relevance-high', type=float, default=0.5)
    parser.add_argument('--relevance-low', type=float, default=0.0)
    parser.add_argument('--binary-relevance', action='store_true')
    parser.add_argument(
        '--answerer',
        default='reference',
        choices=('none', 'reference', 'product'),
        help="Who answers the answer-scored (aggregation) queries. 'none' declines every "
        "one of them — the honest pre-Stage-4 product floor. 'reference' is the harness's "
        'own aggs+SQL control; it is NOT the chat path, and the results file says so. '
        "'product' drives the REAL chat aggregation path (router + aggregation_service) "
        "and is the Stage 4 number: read it against the reference's ceiling and the "
        "null answerer's floor, never on its own.",
    )
    parser.add_argument(
        '--answer-count-tolerance',
        type=int,
        default=0,
        help='Absolute slack allowed on a count. 0: a count is exact.',
    )
    parser.add_argument(
        '--answer-set-credit',
        default='f1',
        choices=('f1', 'exact'),
        help="What 'partial' means for a file-set answer. EM (the gate) is set equality "
        'either way — a subset is never exact.',
    )
    parser.add_argument('--compare', default=None, help='Baseline metrics.json to diff against')
    parser.add_argument(
        '--compare-only',
        nargs=2,
        default=None,
        metavar=('BASELINE_A', 'BASELINE_B'),
        help='Paired-significance diff between two COMMITTED baselines (#461). Each arg is a '
        'baseline name under tests/eval/baselines/, or a path to a baseline dir or its '
        'metrics.json. Reads two files and exits — no OpenSearch, Postgres, or corpus '
        'injection, so it runs with no stack up.',
    )
    parser.add_argument(
        '--host',
        default='localhost',
        help='Host for postgres/opensearch/redis/minio. The harness normally runs in the '
        "host venv against a --fresh stack's published ports, so this is not the "
        'container-network name in .env.',
    )
    parser.add_argument('--allow-live-stack', action='store_true')
    parser.add_argument(
        '--expect-files',
        type=int,
        default=0,
        help='Refuse to measure until this many corpus files carry chunks AND the '
        "count is stable across two polls. 0 = the manifests' own file count. "
        'Pass -1 to skip the settle check entirely (measuring whatever is there).',
    )
    parser.add_argument('--settle-timeout', type=float, default=1800.0)
    parser.add_argument('-v', '--verbose', action='store_true')
    return parser


def _resolve_user_id(email: str) -> int:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.db.base import engine
    from app.models.user import User

    with Session(engine) as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            raise SystemExit(f'No user with email {email!r} on the target stack.')
        return int(user.id)


def _load_corpus(key: str, manifest_root: Path, data_dir: Path):
    """Manifest + queries + turns for one corpus key."""
    from tests.eval.harness import corpora as corpora_mod

    manifest_dir = manifest_root / key
    if not (manifest_dir / 'manifest.json').is_file():
        raise SystemExit(
            f'No injection manifest at {manifest_dir}. Inject the corpus first:\n'
            f'  ./scripts/inject-eval-corpus.sh --fresh <name> --corpus {key}'
        )
    corpus = corpora_mod.load_manifest(manifest_dir)
    turns = corpora_mod.load_turns(manifest_dir)
    if key == 'qmsum':
        queries = corpora_mod.load_qmsum_queries(corpus)
    elif key == 'synthetic':
        # The manifest records the exact directory that was injected, which is a
        # *rung* under $RAG_EVAL_DATA_DIR/synthetic (otsynth-core-v1/, ...), not
        # that directory itself. Prefer it, and fall back to the data dir only so
        # a manifest written on another machine still resolves.
        source = (
            corpus.root if (corpus.root / 'queries.jsonl').is_file() else data_dir / 'synthetic'
        )
        if not (source / 'queries.jsonl').is_file():
            raise SystemExit(
                f'No queries.jsonl under {corpus.root} or {data_dir / "synthetic"}. '
                f'The synthetic corpus must be readable to resolve its gold sets.'
            )
        queries = corpora_mod.load_synthetic_queries(corpus, source)
    elif key == 'miracl':
        # The language is recorded per meeting at injection time; every passage in
        # one manifest shares it. Read it from the manifest rather than taking a
        # flag, so scoring cannot silently be pointed at a different language from
        # the one that was injected.
        languages = {
            str(extra.get('miracl_language') or '') for extra in corpus.extra_by_meeting.values()
        } - {''}
        if len(languages) != 1:
            raise SystemExit(
                f'Expected exactly one MIRACL language in {manifest_dir}, found '
                f'{sorted(languages) or "none"}. Inject one language per manifest — '
                'mixing them would average nDCG across languages that are not comparable.'
            )
        queries = corpora_mod.load_miracl_queries(corpus, languages.pop())
    else:
        raise SystemExit(f'No query loader for corpus {key!r} (it ships no relevance judgements).')
    return corpus, turns, queries


def _fingerprint(entries: list[dict[str, Any]]) -> str:
    """A stable, order-independent digest of an injection-identity entry list."""
    import hashlib

    canonical = json.dumps(entries, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]


def _scan_injection_identity(
    client: Any, index: str, manifest_root: Path, scored_keys: set[str]
) -> dict[str, Any]:
    """Every corpus manifest actually present in the measured index, scored or not.

    ``--corpus`` only lists what gets SCORED — a haystack-only corpus like ``ami`` ships
    no query loader (see the ``else`` branch in :func:`_load_corpus`) and can never appear
    there — but a distractor corpus injected alongside a scored one still changes what
    every retrieval number MEASURES: the haystack a query has to be found IN. Two runs
    with the same scored corpus and a different distractor composition are not
    comparable, and nothing about ``metrics.json`` would show that on its own; this is
    what ``--compare-only`` refuses on rather than warns about (see ``_run_compare_only``
    and ``rag-evaluation.md``'s "AMI distractor haystack" section).

    Recorded in ``runinfo.json``, never ``metrics.json`` — like every other
    run-circumstance field, it is not part of the deterministic scoring claim.

    A manifest directory on disk with ZERO of its files present in the measured index is
    excluded rather than recorded as ``files_present_in_index: 0`` — it may be a manifest
    left over from an unrelated injection (a different ``--fresh`` stack, a deleted
    corpus), and an empty entry would be indistinguishable from "present but retrieved
    nothing" in a later read of the file. This is also the mechanism that keeps stale
    sibling manifests out of a fresh, empty-at-first ``--fresh`` stack's identity: only
    what actually landed in THIS index counts.
    """
    from tests.eval.harness import corpora as corpora_mod
    from tests.eval.harness import index_reader as index_reader_mod

    entries: list[dict[str, Any]] = []
    if manifest_root.is_dir():
        for child in sorted(manifest_root.iterdir()):
            if not (child / 'manifest.json').is_file():
                continue
            try:
                corpus = corpora_mod.load_manifest(child)
            except (OSError, ValueError, KeyError) as exc:
                logger.warning('injection_identity: could not read %s: %s', child, exc)
                continue
            chunks = index_reader_mod.fetch_chunks(client, index, corpus.file_uuids)
            present = sum(1 for v in chunks.values() if v)
            if present == 0:
                continue
            entries.append(
                {
                    'key': corpus.key,
                    'version': corpus.version,
                    'meetings_in_manifest': len(corpus.file_uuid_by_meeting),
                    'files_present_in_index': present,
                    'scored': corpus.key in scored_keys,
                }
            )
    entries.sort(key=lambda e: str(e['key']))
    return {'corpora': entries, 'fingerprint': _fingerprint(entries)}


def _refuse_on_differing_injection_identity(name_a: str, name_b: str) -> str | None:
    """``None`` if safe to compare; otherwise the refusal message for ``--compare-only``.

    Reads each baseline's ``runinfo.json`` (same directory as its ``metrics.json`` — see
    ``_resolve_baseline_metrics_path``). Refuses outright when BOTH sides recorded an
    identity and they differ — a distractor-extended run compared against a QMSum-only
    run would report a delta that is actually a haystack-composition change, and #461's
    own instruction is that this must refuse, not warn. When either side predates this
    field (no ``runinfo.json``, or one without ``injection_identity`` — every baseline
    committed before this landed), comparison proceeds with a loud warning instead: there
    is no fingerprint to disagree with, and refusing every legacy baseline would be a
    regression, not a safety improvement.
    """

    def _load_identity(name: str) -> dict[str, Any] | None:
        runinfo_path = _resolve_baseline_metrics_path(name).with_name('runinfo.json')
        if not runinfo_path.is_file():
            return None
        try:
            runinfo = json.loads(runinfo_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        identity = runinfo.get('injection_identity') if isinstance(runinfo, dict) else None
        return identity if isinstance(identity, dict) else None

    identity_a = _load_identity(name_a)
    identity_b = _load_identity(name_b)
    if identity_a is None or identity_b is None:
        logger.warning(
            '--compare-only: injection identity unavailable for %r and/or %r (predates '
            '#461 A5) — comparison proceeding WITHOUT verifying the two runs measured '
            'the same corpus composition.',
            name_a,
            name_b,
        )
        return None
    if identity_a.get('fingerprint') != identity_b.get('fingerprint'):
        return (
            f'--compare-only: {name_a!r} and {name_b!r} were measured against DIFFERENT '
            f'injected corpus compositions — refusing rather than reporting a delta that '
            f'is actually a haystack change.\n'
            f'  {name_a}: {identity_a.get("corpora")}\n'
            f'  {name_b}: {identity_b.get("corpora")}'
        )
    return None


def _score_answers(args, queries: list, user_id: int, client, settings):
    """Score the answer-scored queries, and record what produced each answer.

    Returns:
        ``(answers_block, answer_rows)``. The block is self-describing: it names
        the answerer, its mechanism per intent, and the scoring policy, so an EM
        value in it cannot be read without its provenance. Answering is serial
        and in query-id order — the aggregations are cheap, and a thread pool
        would buy wall clock at the cost of the one property that matters here.
    """
    from tests.eval.harness import answerers as answerers_mod
    from tests.eval.harness import report as report_mod
    from tests.eval.harness.answers import AnswerPolicy, evaluate_answers, scoring_provenance

    policy = AnswerPolicy(
        count_tolerance=args.answer_count_tolerance, set_credit=args.answer_set_credit
    )
    if not queries:
        return {
            'scored': 0,
            'note': 'no answer-scored queries resolved onto this stack',
            'scoring': scoring_provenance(policy),
        }, []

    from app.db.base import engine

    answerer = answerers_mod.build_answerer(
        args.answerer,
        client=client,
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        user_id=user_id,
        engine=engine,
    )
    gold = {q.query_id: q.gold_answer for q in queries if q.gold_answer is not None}
    submitted = {
        query.query_id: answerer.answer(query)
        for query in sorted(queries, key=lambda q: q.query_id)
    }
    result = evaluate_answers(gold, submitted, policy=policy)
    rows = report_mod.build_answer_rows(queries, result)
    logger.info(
        'Answers (%s): %d scored, %d unanswered, EM %.4f',
        answerer.name,
        result.query_count,
        len(result.unanswered),
        result.aggregate['EM'],
    )
    return {
        'scored': result.query_count,
        'unanswered': len(result.unanswered),
        'answerer': answerer.describe(),
        'scoring': scoring_provenance(policy),
        'rows': rows,
        'details': report_mod.build_answer_details(queries, result),
    }, rows


def _resolve_baseline_metrics_path(baseline: str) -> Path:
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


def _load_baseline_metrics(baseline: str) -> dict[str, Any]:
    path = _resolve_baseline_metrics_path(baseline)
    if not path.is_file():
        raise SystemExit(f'--compare-only: no metrics.json at {path}')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise SystemExit(f'--compare-only: {path} is not a JSON object at the top level')
    return payload


def _render_significance_table(rows: list[dict[str, Any]]) -> str:
    """The ``--compare-only`` table: one line per (corpus, class, measure).

    ``dir`` reads ``↑`` for a higher-is-better measure (nDCG, recall, MRR, ...) and
    ``↓`` for a lower-is-better one (``false_attribution_rate`` — #461 W2.E1). It is
    display-only: `higher_is_better` never changes the sign of `delta_mean` itself,
    only how a reader should interpret a positive one.

    The CI column header is DERIVED from ``row["confidence"]`` (``significance.
    summarize`` carries it on every row precisely so this never hardcodes "95%" —
    a mutant that silently changed the bootstrap's actual confidence level would
    otherwise mislabel its own output). A ``degenerate`` row (n < 2, or every
    delta identical — see ``significance.summarize``'s docstring) is marked with
    ``*``: its CI/p-value do not mean what they would over real spread, and a
    reader scanning only ``ci_contains_zero`` must not mistake it for a real
    result on a class this small.
    """
    confidence = rows[0]['confidence'] if rows else 0.95
    ci_header = f'{confidence * 100:.0f}% CI'
    header = ['corpus', 'class', 'measure', 'dir', 'n', 'delta_mean', ci_header, 't', 'p']
    lines = [
        '| ' + ' | '.join(header) + ' |',
        '|' + '|'.join(['---'] * len(header)) + '|',
    ]
    any_degenerate = False
    for row in rows:
        ci = f'[{row["ci_low"]:+.4f}, {row["ci_high"]:+.4f}]'
        if row['ci_contains_zero']:
            ci += ' (contains 0)'
        t_stat = 'n/a' if row['t_statistic'] is None else f'{row["t_statistic"]:.3f}'
        p_val = 'n/a' if row['p_value'] is None else f'{row["p_value"]:.4f}'
        direction = '↑' if row.get('higher_is_better', True) else '↓'
        degenerate = row.get('degenerate', False)
        any_degenerate = any_degenerate or degenerate
        n_cell = f'{row["n"]}*' if degenerate else str(row['n'])
        lines.append(
            '| '
            + ' | '.join(
                [
                    row['corpus'],
                    row['query_class'],
                    row['measure'],
                    direction,
                    n_cell,
                    f'{row["delta_mean"]:+.4f}',
                    ci,
                    t_stat,
                    p_val,
                ]
            )
            + ' |'
        )
    table = '\n'.join(lines) + '\n'
    if any_degenerate:
        table += (
            '\n* degenerate: n < 2 or every delta identical — CI/p-value are not '
            'meaningful at this sample size, do not gate a decision on them.\n'
        )
    return table


def _run_compare_only(args: argparse.Namespace) -> int:
    """``--compare-only A B``: paired significance between two COMMITTED baselines.

    Reads two ``metrics.json`` files and computes it (#461) — no OpenSearch, no
    Postgres, no corpus injection, so this runs with no stack up at all.
    """
    from tests.eval.harness.significance import PartialJoinError, paired_join, summarize

    name_a, name_b = args.compare_only
    metrics_a = _load_baseline_metrics(name_a)
    metrics_b = _load_baseline_metrics(name_b)

    refusal = _refuse_on_differing_injection_identity(name_a, name_b)
    if refusal is not None:
        logger.error('%s', refusal)
        return 3

    rows_a = metrics_a.get('retrieval_per_query') or []
    rows_b = metrics_b.get('retrieval_per_query') or []
    if not rows_a or not rows_b:
        empty = name_a if not rows_a else name_b
        logger.error(
            '--compare-only: %r has no retrieval_per_query rows — it predates '
            'report.build_retrieval_per_query (8117e6f3) or was regenerated before it, '
            'see rag-evaluation.md#paired-significance',
            empty,
        )
        return 3

    try:
        paired = paired_join(rows_a, rows_b)
    except PartialJoinError as exc:
        logger.error('--compare-only: %s', exc)
        return 3

    # The baseline ARGUMENT identifies which file was read; metrics.json's own
    # `control_name` is whatever --control-name the run was invoked with (often
    # left at its default), so printing that instead would show 'stage1-baseline'
    # for two baselines the caller explicitly told apart by name.
    summary_rows = summarize(paired)
    print(f'Paired significance: {name_a!r} (A) vs {name_b!r} (B), {len(paired)} queries')
    print(_render_significance_table(summary_rows))
    return 0


def main(argv: list[str] | None = None) -> int:  # noqa: C901 — a CLI, read top to bottom
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s %(message)s'
    )
    logging.getLogger('app.services.search.chunk_retrieval').setLevel(logging.WARNING)

    if args.compare_only:
        # No stack needed: reads two committed baseline files and exits, before any
        # of the OpenSearch/Postgres/corpus-injection setup below.
        return _run_compare_only(args)

    # setdefault, not assignment: an explicitly exported host still wins, which
    # is how the ./opentr.sh wrapper and CI point this somewhere else.
    for var in ('POSTGRES_HOST', 'OPENSEARCH_HOST', 'REDIS_HOST', 'MINIO_HOST'):
        os.environ.setdefault(var, args.host)

    # Imported here rather than at module scope because it is only resolvable
    # after the sys.path insert at the top of this file. At module scope it is
    # an E402 that can be silenced but not fixed; in here it is simply correct,
    # and it is the same rule the harness imports below already follow.
    from app.scripts.corpus_injection.env import (
        LiveStackRefusedError,
        bootstrap,
        describe_target,
        guard_live_stack,
    )

    bootstrap(REPO_ROOT)
    try:
        guard_live_stack(allow=args.allow_live_stack)
    except LiveStackRefusedError as exc:
        logger.error('%s', exc)
        return 2

    # Imports below must follow bootstrap(): app.core.config reads os.environ at
    # import time, and tests.eval.harness pulls app.services in through runner.
    from tests.eval.harness import index_reader
    from tests.eval.harness import metrics as metrics_mod
    from tests.eval.harness import report as report_mod
    from tests.eval.harness import runner as runner_mod
    from tests.eval.harness.qrels import QrelsBuilder, RelevancePolicy

    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client

    started = time.monotonic()
    target = describe_target()
    logger.info('Target: opensearch=%s postgres=%s', target['opensearch'], target['postgres'])

    keys = args.corpus or ['qmsum']
    manifest_root = Path(args.manifest_root)
    data_dir = Path(args.data_dir)
    user_id = _resolve_user_id(args.user)

    policy = RelevancePolicy(
        high=args.relevance_high, low=args.relevance_low, binary=args.binary_relevance
    )
    config = runner_mod.RetrievalConfig(
        stage=args.stage,
        size=args.size,
        search_mode=args.search_mode,
        scope=args.scope,
        workers=args.workers,
        fusion=_build_fusion(args),
        text_fields_preset=args.bm25_fields,
        **_build_budget(args),
    )

    client = get_opensearch_client()
    if client is None:
        raise SystemExit('No OpenSearch client — is the stack up and are the ports exported?')

    # Load every corpus BEFORE touching the index: the settle check needs the
    # complete expected file set, and measuring a corpus that is still being
    # indexed is the failure this whole preamble exists to prevent.
    loaded = [(key, *_load_corpus(key, manifest_root, data_dir)) for key in keys]
    settled: dict[str, Any] | None = None
    if args.expect_files >= 0:
        corpus_uuids = sorted({uuid for _, corpus, _, _ in loaded for uuid in corpus.file_uuids})
        expected = args.expect_files or len(corpus_uuids)
        try:
            settled = index_reader.await_settled(
                client,
                settings.OPENSEARCH_CHUNKS_INDEX,
                corpus_uuids,
                expected_files=expected,
                timeout_s=args.settle_timeout,
            )
        except index_reader.IndexNotSettledError as exc:
            logger.error('%s', exc)
            return 3
    index_state = index_reader.prepare_index(client, settings.OPENSEARCH_CHUNKS_INDEX)

    # The embedding model belongs in the CLAIM, not in runinfo.json (#437). A
    # retrieval number is a statement about a corpus AND the model that vectorised
    # it — swap the model and the same code over the same corpus produces a
    # different number, so a baseline that does not name it cannot be compared to
    # anything. The nine baselines committed before this field existed name no
    # model, and — measured, not assumed — the cluster cannot supply one for them
    # either: all 210,908 documents carry `embedding_model: "neural"`, #437's
    # single UNKNOWN bucket. Their model is circumstantial (a 384-dimension index
    # and a configured all-MiniLM-L6-v2), never provable. That is the whole cost
    # of having recorded it late, and why it is recorded here rather than argued
    # about later; `backend/tests/eval/baselines/README.md` classifies each one.
    #
    # Sourced from the DOCUMENTS, not from get_search_embedding_settings(). #437
    # established that the settings are not authoritative about the vectors: two
    # SystemSettings keys (search.embedding_model drives the index dimension,
    # search.opensearch_model_id drives the pipeline) are written by different
    # endpoints with nothing reconciling them, so a settings-derived label can
    # name a model that never touched a single vector in this corpus. The survey
    # aggregates what the indexed documents themselves report, which is the only
    # thing that can be true about a measurement already taken.
    try:
        from app.services.search.embedding_provenance import survey_embedding_models
        from app.services.search.settings_service import get_search_embedding_settings

        provenance = survey_embedding_models(settings.OPENSEARCH_CHUNKS_INDEX)
        configured_model, embedding_dimension = get_search_embedding_settings()
        index_state = {
            **index_state,
            'embedding_models': list(provenance.known_models),
            'embedding_verdict': provenance.verdict,
            'embedding_unattributed': provenance.unattributed,
            'embedding_dimension': embedding_dimension,
            # Kept as a separate, differently-named field precisely so that
            # drift between what is configured and what is indexed is visible
            # in the committed baseline rather than collapsed into one number.
            'configured_embedding_model': configured_model,
        }
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal to a measurement
        # Recorded rather than swallowed: "we could not read the model" and "the
        # model is X" must not look the same in a committed baseline.
        logger.warning('Could not resolve the embedding provenance: %s', exc)
        index_state = {
            **index_state,
            'embedding_models': [],
            'embedding_verdict': 'UNRESOLVED',
            'embedding_dimension': 0,
        }
    else:
        # A mixed corpus makes the measurement meaningless rather than merely
        # unlabelled: cosine between two models is not a similarity, so the
        # ranking being scored fused two incomparable vector populations. Refuse
        # to write a baseline nobody could correctly interpret later.
        if provenance.mixed:
            logger.error('%s', provenance.describe())
            logger.error(
                'Refusing to record a baseline over a mixed vector space — '
                'reindex to one model first (GET /search/models/neural/status).'
            )
            return 3

    if settled is not None:
        # Only the settled counters go in the committed document. How many polls
        # it took is a property of when the run started, not of the corpus, and
        # metrics.json is byte-identical across runs by construction.
        index_state = {
            **index_state,
            'corpus_files': settled['files'],
            'corpus_chunks': settled['chunks'],
            'expected_files': settled['expected_files'],
        }

    all_queries = []
    answer_queries = []
    corpus_records = []
    qrels: dict[str, dict[str, int]] = {}
    unjudged: list[str] = []

    for _key, corpus, turns, queries in loaded:
        queries.sort(key=lambda query: query.query_id)
        if args.limit_queries:
            queries = queries[: args.limit_queries]
        chunks = index_reader.fetch_chunks(
            client, settings.OPENSEARCH_CHUNKS_INDEX, corpus.file_uuids
        )
        builder = QrelsBuilder(turns, chunks, policy)
        scored = 0
        answer_scored = 0
        for query in queries:
            # Two engines, two query sets. An aggregation query's ground truth is
            # an integer or a file set; no ranking metric can express it, and
            # scoring it as though one could is what left the class unmeasured.
            if query.scored_on == 'answer':
                answer_queries.append(query)
                answer_scored += 1
                continue
            judged = builder.judgements(list(query.spans))
            if not judged:
                # A query whose gold span maps to no chunk cannot discriminate
                # anything; keeping it would score every system 0 and drag every
                # mean by the same amount. Counted and reported instead.
                unjudged.append(query.query_id)
                continue
            qrels[query.query_id] = judged
            all_queries.append(query)
            scored += 1
        corpus_records.append(
            {
                'key': corpus.key,
                'name': corpus.name,
                'version': corpus.version,
                'license_tier': corpus.license_tier,
                'files_indexed': len(chunks),
                'files_in_manifest': len(corpus.file_uuid_by_meeting),
                'chunks_indexed': sum(len(v) for v in chunks.values()),
                'queries_scored': scored,
                'queries_dropped_unjudgeable': len(queries) - scored - answer_scored,
                'answer_queries_scored': answer_scored,
            }
        )
        logger.info(
            '%s: %d files, %d chunks, %d retrieval queries, %d answer queries',
            corpus.key,
            len(chunks),
            sum(len(v) for v in chunks.values()),
            scored,
            answer_scored,
        )

    if not all_queries and not answer_queries:
        raise SystemExit('No scoreable queries — nothing to measure.')

    rows: list[dict] = []
    # Initialised beside `rows` for the same reason: both are only assigned
    # inside the branch below, and build_results consumes both unconditionally.
    retrieval_per_query: list[dict] = []
    result = None
    route_records: dict[str, runner_mod.RouteRecord] = {}
    digest_leg = None
    retrieval_ms: list[float] = []
    if all_queries:
        run = runner_mod.execute(
            all_queries,
            user_id=user_id,
            config=config,
            records=route_records if config.stage == 'route' else None,
            retrieval_ms=retrieval_ms,
        )
        result = metrics_mod.evaluate(qrels, run)
        rows = report_mod.build_rows(all_queries, result)
        # #461 phase 0: the per-query scores were already computed and thrown away
        # for retrieval queries, so every published number was a point estimate
        # nobody could put an interval around.
        retrieval_per_query = report_mod.build_retrieval_per_query(all_queries, result)
        if config.stage == 'route':
            from tests.eval.harness.routing import build_digest_leg_report

            digest_leg = build_digest_leg_report(all_queries, route_records)
            logger.info(
                'Digest leg: %d/%d queries routed to it, %d rescued a gold file '
                'the chunk leg missed',
                digest_leg['routed_to_digest_tier'],
                digest_leg['queries_scored'],
                digest_leg['rescued']['queries'],
            )

    answers_block, answer_rows = _score_answers(args, answer_queries, user_id, client, settings)

    judged_counts = [len(v) for v in qrels.values()] or [0]
    results = report_mod.build_results(
        control_name=args.control_name,
        corpora=corpus_records,
        retrieval=config.as_dict(),
        policy=policy.as_dict(),
        index_state=index_state,
        qrels_stats={
            'queries': len(qrels),
            'judged_documents': sum(judged_counts),
            'mean_judged_per_query': round(sum(judged_counts) / len(judged_counts), 4),
            'queries_dropped_unjudgeable': len(unjudged),
            'unanswered_queries': len(result.unanswered) if result is not None else 0,
            'answer_scored_queries_in_their_own_table': len(answer_queries),
        },
        rows=rows,
        retrieval_per_query=retrieval_per_query,
        answers=answers_block,
    )
    if digest_leg is not None:
        results['digest_leg'] = digest_leg

    out_dir = Path(args.out) if args.out else DEFAULT_BASELINE_ROOT / args.control_name
    out_dir.mkdir(parents=True, exist_ok=True)
    table = report_mod.render_table(rows)
    (out_dir / 'metrics.json').write_text(report_mod.dumps(results), encoding='utf-8')
    (out_dir / 'metrics.md').write_text(table, encoding='utf-8')
    if answer_rows:
        answer_table = report_mod.render_answer_table(answer_rows)
        (out_dir / 'answers.md').write_text(answer_table, encoding='utf-8')
    elapsed = time.monotonic() - started
    injection_identity = _scan_injection_identity(
        client, settings.OPENSEARCH_CHUNKS_INDEX, manifest_root, scored_keys=set(keys)
    )
    (out_dir / 'runinfo.json').write_text(
        json.dumps(
            {
                'elapsed_seconds': round(elapsed, 1),
                'target': target,
                'settle': settled,
                'retrieval_latency_ms': _latency_summary(retrieval_ms, config.workers),
                'injection_identity': injection_identity,
            },
            indent=2,
        )
        + '\n',
        encoding='utf-8',
    )

    print(table)
    if answer_rows:
        print(report_mod.render_answer_table(answer_rows))
    if args.compare:
        baseline = json.loads(Path(args.compare).read_text(encoding='utf-8'))
        print(f"\nΔ vs control '{baseline.get('control_name')}':")
        print(report_mod.render_comparison(baseline.get('rows') or [], rows))
    logger.info('Wrote %s (%.1fs)', out_dir / 'metrics.json', elapsed)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
