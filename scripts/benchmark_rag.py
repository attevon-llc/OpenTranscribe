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

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
# `backend` first: it is what makes both `app.*` and `tests.eval.harness.*`
# importable from a script that lives outside it.
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.scripts.corpus_injection.env import LiveStackRefusedError  # noqa: E402
from app.scripts.corpus_injection.env import bootstrap  # noqa: E402
from app.scripts.corpus_injection.env import describe_target  # noqa: E402
from app.scripts.corpus_injection.env import guard_live_stack  # noqa: E402

logger = logging.getLogger("benchmark_rag")

DEFAULT_MANIFEST_ROOT = REPO_ROOT / ".rag-403" / "injections"
DEFAULT_BASELINE_ROOT = BACKEND / "tests" / "eval" / "baselines"
DEFAULT_DATA_DIR = Path("/mnt/nas/opentranscribe-benchmarks")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        action="append",
        default=None,
        help="Corpus key to score; repeatable. Default: qmsum",
    )
    parser.add_argument("--user", default="admin@example.com", help="Owner of the injected corpus")
    parser.add_argument("--manifest-root", default=str(DEFAULT_MANIFEST_ROOT))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--control-name", default="stage1-baseline")
    parser.add_argument("--out", default=None, help="Output dir [tests/eval/baselines/<name>]")
    parser.add_argument("--stage", default="retrieve", choices=("retrieve", "rerank"))
    parser.add_argument(
        "--scope",
        default="corpus",
        choices=("corpus", "gold-files"),
        help="corpus = what chat does; gold-files = ORACLE file selection (upper bound)",
    )
    parser.add_argument("--search-mode", default="hybrid", choices=("hybrid", "semantic", "keyword"))
    parser.add_argument("--size", type=int, default=48, help="Candidate pool per query")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent retrieval requests. Results are keyed by query id, so "
        "this changes wall clock and nothing else.",
    )
    parser.add_argument("--limit-queries", type=int, default=0, help="Score at most N per corpus")
    parser.add_argument("--relevance-high", type=float, default=0.5)
    parser.add_argument("--relevance-low", type=float, default=0.0)
    parser.add_argument("--binary-relevance", action="store_true")
    parser.add_argument("--compare", default=None, help="Baseline metrics.json to diff against")
    parser.add_argument(
        "--host",
        default="localhost",
        help="Host for postgres/opensearch/redis/minio. The harness normally runs in the "
        "host venv against a --fresh stack's published ports, so this is not the "
        "container-network name in .env.",
    )
    parser.add_argument("--allow-live-stack", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _resolve_user_id(email: str) -> int:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.db.base import engine
    from app.models.user import User

    with Session(engine) as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            raise SystemExit(f"No user with email {email!r} on the target stack.")
        return int(user.id)


def _load_corpus(key: str, manifest_root: Path, data_dir: Path):
    """Manifest + queries + turns for one corpus key."""
    from tests.eval.harness import corpora as corpora_mod

    manifest_dir = manifest_root / key
    if not (manifest_dir / "manifest.json").is_file():
        raise SystemExit(
            f"No injection manifest at {manifest_dir}. Inject the corpus first:\n"
            f"  ./scripts/inject-eval-corpus.sh --fresh <name> --corpus {key}"
        )
    corpus = corpora_mod.load_manifest(manifest_dir)
    turns = corpora_mod.load_turns(manifest_dir)
    if key == "qmsum":
        queries = corpora_mod.load_qmsum_queries(corpus)
    elif key == "synthetic":
        queries = corpora_mod.load_synthetic_queries(corpus, data_dir / "synthetic")
    else:
        raise SystemExit(f"No query loader for corpus {key!r} (it ships no relevance judgements).")
    return corpus, turns, queries


def main(argv: list[str] | None = None) -> int:  # noqa: C901 — a CLI, read top to bottom
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s"
    )
    logging.getLogger("app.services.search.chunk_retrieval").setLevel(logging.WARNING)

    # setdefault, not assignment: an explicitly exported host still wins, which
    # is how the ./opentr.sh wrapper and CI point this somewhere else.
    for var in ("POSTGRES_HOST", "OPENSEARCH_HOST", "REDIS_HOST", "MINIO_HOST"):
        os.environ.setdefault(var, args.host)

    bootstrap(REPO_ROOT)
    try:
        guard_live_stack(allow=args.allow_live_stack)
    except LiveStackRefusedError as exc:
        logger.error("%s", exc)
        return 2

    # Imports below must follow bootstrap(): app.core.config reads os.environ at
    # import time, and tests.eval.harness pulls app.services in through runner.
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from tests.eval.harness import index_reader
    from tests.eval.harness import metrics as metrics_mod
    from tests.eval.harness import report as report_mod
    from tests.eval.harness import runner as runner_mod
    from tests.eval.harness.qrels import QrelsBuilder
    from tests.eval.harness.qrels import RelevancePolicy

    started = time.monotonic()
    target = describe_target()
    logger.info("Target: opensearch=%s postgres=%s", target["opensearch"], target["postgres"])

    keys = args.corpus or ["qmsum"]
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
    )

    client = get_opensearch_client()
    if client is None:
        raise SystemExit("No OpenSearch client — is the stack up and are the ports exported?")
    index_state = index_reader.prepare_index(client, settings.OPENSEARCH_CHUNKS_INDEX)

    all_queries = []
    corpus_records = []
    qrels: dict[str, dict[str, int]] = {}
    unjudged: list[str] = []

    for key in keys:
        corpus, turns, queries = _load_corpus(key, manifest_root, data_dir)
        queries.sort(key=lambda query: query.query_id)
        if args.limit_queries:
            queries = queries[: args.limit_queries]
        chunks = index_reader.fetch_chunks(
            client, settings.OPENSEARCH_CHUNKS_INDEX, corpus.file_uuids
        )
        builder = QrelsBuilder(turns, chunks, policy)
        scored = 0
        for query in queries:
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
                "key": corpus.key,
                "name": corpus.name,
                "version": corpus.version,
                "license_tier": corpus.license_tier,
                "files_indexed": len(chunks),
                "files_in_manifest": len(corpus.file_uuid_by_meeting),
                "chunks_indexed": sum(len(v) for v in chunks.values()),
                "queries_scored": scored,
                "queries_dropped_unjudgeable": len(queries) - scored,
            }
        )
        logger.info(
            "%s: %d files, %d chunks, %d scoreable queries",
            corpus.key,
            len(chunks),
            sum(len(v) for v in chunks.values()),
            scored,
        )

    if not all_queries:
        raise SystemExit("No scoreable queries — nothing to measure.")

    run = runner_mod.execute(all_queries, user_id=user_id, config=config)
    result = metrics_mod.evaluate(qrels, run)
    rows = report_mod.build_rows(all_queries, result)

    judged_counts = [len(v) for v in qrels.values()]
    results = report_mod.build_results(
        control_name=args.control_name,
        corpora=corpus_records,
        retrieval=config.as_dict(),
        policy=policy.as_dict(),
        index_state=index_state,
        qrels_stats={
            "queries": len(qrels),
            "judged_documents": sum(judged_counts),
            "mean_judged_per_query": round(sum(judged_counts) / len(judged_counts), 4),
            "queries_dropped_unjudgeable": len(unjudged),
            "unanswered_queries": len(result.unanswered),
        },
        rows=rows,
    )

    out_dir = Path(args.out) if args.out else DEFAULT_BASELINE_ROOT / args.control_name
    out_dir.mkdir(parents=True, exist_ok=True)
    table = report_mod.render_table(rows)
    (out_dir / "metrics.json").write_text(report_mod.dumps(results), encoding="utf-8")
    (out_dir / "metrics.md").write_text(table, encoding="utf-8")
    elapsed = time.monotonic() - started
    (out_dir / "runinfo.json").write_text(
        json.dumps({"elapsed_seconds": round(elapsed, 1), "target": target}, indent=2) + "\n",
        encoding="utf-8",
    )

    print(table)
    if args.compare:
        baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print(f"\nΔ vs control '{baseline.get('control_name')}':")
        print(report_mod.render_comparison(baseline.get("rows") or [], rows))
    logger.info("Wrote %s (%.1fs)", out_dir / "metrics.json", elapsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
