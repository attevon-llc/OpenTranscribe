#!/usr/bin/env python3
"""Block until an injected eval corpus has actually settled in OpenSearch.

    python3 scripts/verify-eval-corpus-settled.py --manifest-dir .rag-403/injections/qmsum

Called automatically by ``inject-eval-corpus.sh`` after a successful injection.
It exists because the injector (``app.scripts.corpus_injection``) dispatches the
production ``index_transcript_search`` task per file and exits 0 the moment every
*dispatch call* returns — regardless of whether a single one of those tasks ever
completed. Measured directly: a stack where every embedding task crashed (a stale
``Settings`` singleton in a long-lived Celery worker) printed
``Done: 232 meetings (232 created...)`` and exited 0 while the
``transcript_chunks`` index was never created. Nothing surfaced it; it was found
only by manually polling the chunk count and then reading the worker's log.

Reuses the eval harness's own settle rule
(``tests.eval.harness.index_reader.await_settled``) rather than a third
implementation — see that module's docstring for why polling the chunk total
alone is not enough (it produced phantom deltas of 223/357/591 chunks over an
unchanged corpus in this exact codebase).

Exits 0 only once every file the manifest lists carries at least one chunk and
two consecutive polls agree the corpus has stopped changing. Exits 1 on timeout
(a broken worker fails in minutes, not forever — see ``--timeout``), 2 on a setup
problem (no manifest, no OpenSearch client).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

logger = logging.getLogger('verify_eval_corpus_settled')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--manifest-dir',
        required=True,
        help="Directory holding this injection's manifest.json/files.jsonl "
        "(what corpus_injection just wrote — the 'Manifest: ...' line it logs)",
    )
    parser.add_argument(
        '--timeout',
        type=float,
        default=1800.0,
        help='Give up after this many seconds so a broken worker fails in minutes, '
        'not forever [1800]',
    )
    parser.add_argument('-v', '--verbose', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s %(message)s'
    )

    # Imported here, not at module scope: it resolves through the sys.path.insert
    # above, so a top-level import raises E402 (see scripts/reindex_eval_corpus.py,
    # which does the same for the same reason).
    from app.scripts.corpus_injection.env import bootstrap

    bootstrap(REPO_ROOT)

    from tests.eval.harness import corpora as corpora_mod
    from tests.eval.harness import index_reader

    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client

    manifest_dir = Path(args.manifest_dir)
    if not (manifest_dir / 'manifest.json').is_file():
        logger.error('No manifest.json under %s — nothing to verify.', manifest_dir)
        return 2

    corpus = corpora_mod.load_manifest(manifest_dir)
    file_uuids = corpus.file_uuids
    if not file_uuids:
        logger.warning('Manifest at %s lists no files — nothing to settle.', manifest_dir)
        return 0

    client = get_opensearch_client()
    if client is None:
        logger.error('No OpenSearch client — is the stack up and are the ports exported?')
        return 2

    logger.info(
        "Waiting for %d file(s) from corpus '%s' to settle in %s (timeout %.0fs)",
        len(file_uuids),
        corpus.key,
        settings.OPENSEARCH_CHUNKS_INDEX,
        args.timeout,
    )
    started = time.monotonic()
    try:
        settled = index_reader.await_settled(
            client,
            settings.OPENSEARCH_CHUNKS_INDEX,
            file_uuids,
            expected_files=len(file_uuids),
            timeout_s=args.timeout,
        )
    except index_reader.IndexNotSettledError as exc:
        logger.error('%s', exc)
        return 1
    elapsed = time.monotonic() - started
    print(
        json.dumps(
            {'corpus': corpus.key, 'elapsed_seconds': round(elapsed, 1), **settled},
            indent=2,
            sort_keys=True,
        )
    )
    logger.info(
        'Settled: %d/%d files, %d chunks in %.0fs',
        settled['files'],
        len(file_uuids),
        settled['chunks'],
        elapsed,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
