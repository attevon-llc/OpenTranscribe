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

from rag_bench.config import DEFAULT_DATA_DIR, DEFAULT_MANIFEST_ROOT
from rag_bench.fusion import add_fusion_arguments


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
    add_fusion_arguments(parser)
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
