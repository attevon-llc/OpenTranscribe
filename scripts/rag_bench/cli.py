"""The retrieval benchmark run: wire the stack, retrieve, score, write the baseline."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from rag_bench.compare import run_compare_only
from rag_bench.config import DEFAULT_BASELINE_ROOT, REPO_ROOT, logger
from rag_bench.corpus import load_corpus, resolve_user_id, scan_injection_identity
from rag_bench.fusion import build_budget, build_fusion
from rag_bench.parser import build_parser
from rag_bench.scoring import latency_summary, score_answers


def main(argv: list[str] | None = None) -> int:  # noqa: C901 — a CLI, read top to bottom
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s %(message)s'
    )
    logging.getLogger('app.services.search.chunk_retrieval').setLevel(logging.WARNING)

    if args.compare_only:
        # No stack needed: reads two committed baseline files and exits, before any
        # of the OpenSearch/Postgres/corpus-injection setup below.
        return run_compare_only(args)

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
    user_id = resolve_user_id(args.user)

    policy = RelevancePolicy(
        high=args.relevance_high, low=args.relevance_low, binary=args.binary_relevance
    )
    config = runner_mod.RetrievalConfig(
        stage=args.stage,
        size=args.size,
        search_mode=args.search_mode,
        scope=args.scope,
        workers=args.workers,
        fusion=build_fusion(args),
        text_fields_preset=args.bm25_fields,
        **build_budget(args),
    )

    client = get_opensearch_client()
    if client is None:
        raise SystemExit('No OpenSearch client — is the stack up and are the ports exported?')

    # Load every corpus BEFORE touching the index: the settle check needs the
    # complete expected file set, and measuring a corpus that is still being
    # indexed is the failure this whole preamble exists to prevent.
    loaded = [(key, *load_corpus(key, manifest_root, data_dir)) for key in keys]
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

    answers_block, answer_rows = score_answers(args, answer_queries, user_id, client, settings)

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
    injection_identity = scan_injection_identity(
        client, settings.OPENSEARCH_CHUNKS_INDEX, manifest_root, scored_keys=set(keys)
    )
    (out_dir / 'runinfo.json').write_text(
        json.dumps(
            {
                'elapsed_seconds': round(elapsed, 1),
                'target': target,
                'settle': settled,
                'retrieval_latency_ms': latency_summary(retrieval_ms, config.workers),
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
