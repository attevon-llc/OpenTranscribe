#!/usr/bin/env python3
"""Re-index an injected eval corpus through the production path, and wait for it.

    python3 scripts/reindex_eval_corpus.py --host localhost --user admin@example.com

Dispatches the real `reindex_transcripts` Celery task — the same coordinator the
admin UI triggers — so the corpus is rebuilt by the **worker**, with the worker's
sentence splitter, its embedding pipeline and its `_INDEX_VERSION` handling. An
in-process rebuild would measure this host instead (issue #436: the two disagree).

Then it blocks until the corpus has settled, using the harness's own settle rule
(`tests/eval/harness/index_reader.await_settled`), and prints the resulting chunk
count. Running it twice and comparing that number is the determinism check #403
Stage 3 must pass before any metric delta means anything.

The corpus is identified by the injection manifests, so this never touches a file
the harness would not measure. It refuses the shared dev stack, like every other
tool in this family.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.scripts.corpus_injection.env import LiveStackRefusedError  # noqa: E402
from app.scripts.corpus_injection.env import bootstrap  # noqa: E402
from app.scripts.corpus_injection.env import describe_target  # noqa: E402
from app.scripts.corpus_injection.env import guard_live_stack  # noqa: E402

logger = logging.getLogger("reindex_eval_corpus")

DEFAULT_MANIFEST_ROOT = REPO_ROOT / ".rag-403" / "injections"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", action="append", default=None, help="Repeatable [all present]")
    parser.add_argument("--user", default="admin@example.com")
    parser.add_argument("--manifest-root", default=str(DEFAULT_MANIFEST_ROOT))
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Skip the reindex and only wait for the corpus to settle.",
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


def _corpus_uuids(manifest_root: Path, keys: list[str] | None) -> tuple[list[str], list[str]]:
    from tests.eval.harness import corpora as corpora_mod

    found = sorted(
        path.parent.name for path in manifest_root.glob("*/manifest.json") if path.is_file()
    )
    selected = [key for key in found if keys is None or key in keys]
    if not selected:
        raise SystemExit(f"No injection manifest under {manifest_root} for {keys or 'any corpus'}.")
    uuids: set[str] = set()
    for key in selected:
        uuids.update(corpora_mod.load_manifest(manifest_root / key).file_uuids)
    return selected, sorted(uuids)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s"
    )
    for var in ("POSTGRES_HOST", "OPENSEARCH_HOST", "REDIS_HOST", "MINIO_HOST"):
        os.environ.setdefault(var, args.host)

    bootstrap(REPO_ROOT)
    try:
        guard_live_stack(allow=args.allow_live_stack)
    except LiveStackRefusedError as exc:
        logger.error("%s", exc)
        return 2

    from app.core.celery import celery_app
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from tests.eval.harness import index_reader

    target = describe_target()
    logger.info("Target: opensearch=%s postgres=%s", target["opensearch"], target["postgres"])

    keys, uuids = _corpus_uuids(Path(args.manifest_root), args.corpus)
    logger.info("Corpora %s: %d files", ",".join(keys), len(uuids))

    client = get_opensearch_client()
    if client is None:
        raise SystemExit("No OpenSearch client — is the stack up and are the ports exported?")

    started = time.monotonic()
    since: str | None = None
    if not args.no_dispatch:
        user_id = _resolve_user_id(args.user)
        # One second back: the worker's clock and this one are the same host, but
        # `indexed_at` is stamped before the bulk load, so an exactly-equal
        # timestamp is legitimately "written by this run".
        since = (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)
        ).isoformat()
        result = celery_app.send_task("reindex_transcripts", kwargs={"user_id": user_id})
        logger.info("Dispatched reindex_transcripts id=%s for user %d", result.id, user_id)

    settled = index_reader.await_settled(
        client,
        settings.OPENSEARCH_CHUNKS_INDEX,
        uuids,
        expected_files=len(uuids),
        since=since,
        timeout_s=args.timeout,
    )
    elapsed = time.monotonic() - started
    print(
        json.dumps(
            {"corpora": keys, "elapsed_seconds": round(elapsed, 1), **settled},
            indent=2,
            sort_keys=True,
        )
    )
    logger.info("Settled: %d files, %d chunks in %.0fs", settled["files"], settled["chunks"], elapsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
