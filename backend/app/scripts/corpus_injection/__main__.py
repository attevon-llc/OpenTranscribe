"""CLI: parse an eval corpus into the app's data model and index it for real.

    python -m app.scripts.corpus_injection --corpus qmsum --user admin@example.com

Run it through ``scripts/inject-eval-corpus.sh``, which resolves the isolated
stack's ports for you. ``--help`` for the full flag list.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.scripts.corpus_injection.env import LiveStackRefusedError
from app.scripts.corpus_injection.env import bootstrap
from app.scripts.corpus_injection.env import describe_target
from app.scripts.corpus_injection.env import guard_live_stack

TOOL_VERSION = "1.0.0"
DEFAULT_DATA_DIR = "/mnt/nas/opentranscribe-benchmarks"

logger = logging.getLogger("corpus_injection")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.corpus_injection",
        description="Inject reference meeting transcripts into OpenTranscribe without ASR, "
        "then dispatch the production search-indexing task.",
    )
    parser.add_argument("--corpus", required=True, help="Corpus key (qmsum, synthetic, ...)")
    parser.add_argument("--data-dir", default=None, help=f"$RAG_EVAL_DATA_DIR [{DEFAULT_DATA_DIR}]")
    parser.add_argument("--corpus-root", default=None, help="Override this corpus's directory")
    parser.add_argument("--user", required=True, help="Owner account email")
    parser.add_argument("--seed", default="", help="Namespace suffix for the derived UUIDs")
    parser.add_argument("--limit", type=int, default=0, help="Inject at most N meetings")
    parser.add_argument("--only", nargs="*", default=None, help="Inject only these meeting ids")
    parser.add_argument(
        "--manifest-dir",
        default=None,
        help="Where to write manifest.json / files.jsonl / turns.jsonl "
        "[<repo>/.rag-403/injections/<corpus>-<seed>]",
    )
    parser.add_argument(
        "--dispatch",
        choices=("celery", "eager", "none"),
        default="celery",
        help="celery: real worker (production path). eager: same task, in-process. none: rows only.",
    )
    parser.add_argument(
        "--min-alignment-rate",
        type=float,
        default=0.8,
        help="Fraction of turns that must align to a timed reference before a meeting's "
        "timings count as real; below it the whole meeting goes synthetic [0.8]",
    )
    parser.add_argument("--force", action="store_true", help="Rewrite rows even if unchanged")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report, write nothing")
    parser.add_argument(
        "--allow-live-stack",
        action="store_true",
        help="Permit a target on the shared dev stack's ports (5176/5178/5180)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _resolve_user(db, email: str) -> int:  # noqa: ANN001 — Session, imported lazily
    from sqlalchemy import select

    from app.models.user import User

    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        raise SystemExit(f"No user with email {email!r} on the target stack.")
    return int(user.id)


def main(argv: list[str] | None = None) -> int:  # noqa: C901 — a CLI, read top to bottom
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s"
    )

    repo_root = bootstrap()
    try:
        guard_live_stack(allow=args.allow_live_stack)
    except LiveStackRefusedError as exc:
        logger.error("%s", exc)
        return 2

    # Imports below must follow bootstrap(): app.core.config reads os.environ
    # at import time.
    from sqlalchemy.orm import Session

    from app.db.base import engine
    from app.scripts.corpus_injection import manifest as manifest_mod
    from app.scripts.corpus_injection.adapters import build_adapter
    from app.scripts.corpus_injection.injector import dispatch_indexing
    from app.scripts.corpus_injection.injector import inject_meeting

    target = describe_target()
    logger.info("Target: postgres=%s opensearch=%s", target["postgres"], target["opensearch"])

    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    adapter = build_adapter(
        args.corpus, data_dir, Path(args.corpus_root) if args.corpus_root else None
    )
    info = adapter.describe()
    logger.info(
        "Corpus: %s version=%s tier=%s root=%s",
        info.name,
        info.version,
        info.license_tier,
        info.root,
    )
    if info.license_tier != "A":
        logger.warning(
            "Tier %s corpus: usable locally, but NO derived metric may be published.",
            info.license_tier,
        )

    meeting_ids = adapter.meeting_ids()
    if args.only:
        meeting_ids = [m for m in meeting_ids if m in set(args.only)]
    if args.limit:
        meeting_ids = meeting_ids[: args.limit]
    logger.info("Injecting %d meeting(s)", len(meeting_ids))

    records = []
    turns_by_file: dict[str, list] = {}
    with Session(engine) as db:
        user_id = _resolve_user(db, args.user)
        for position, meeting_id in enumerate(meeting_ids, start=1):
            doc = adapter.load(meeting_id)
            if args.dry_run:
                from app.scripts.corpus_injection.timings import resolve_timings

                resolve_timings(doc, min_alignment_rate=args.min_alignment_rate)
                logger.info(
                    "[dry-run %d/%d] %s turns=%d words=%d timings=%s",
                    position,
                    len(meeting_ids),
                    meeting_id,
                    len(doc.turns),
                    doc.word_count,
                    doc.timing.source,
                )
                continue

            record, turn_rows = inject_meeting(
                db,
                doc,
                user_id,
                seed=args.seed,
                tool_version=TOOL_VERSION,
                min_alignment_rate=args.min_alignment_rate,
                force=args.force,
            )
            db.commit()
            if record.action != "skipped":
                record.index_task_id = dispatch_indexing(record, user_id, mode=args.dispatch)
            records.append(record)
            turns_by_file[record.file_uuid] = turn_rows
            logger.info(
                "[%d/%d] %s %s segments=%d timings=%s(%.0f%%)",
                position,
                len(meeting_ids),
                record.action,
                meeting_id,
                record.segment_count,
                record.timing_source,
                100 * record.timing_alignment_rate,
            )

    if args.dry_run:
        logger.info("Dry run complete — nothing written.")
        return 0

    manifest_dir = Path(
        args.manifest_dir
        or (
            (repo_root or Path.cwd())
            / ".rag-403"
            / "injections"
            / f"{args.corpus}{'-' + args.seed if args.seed else ''}"
        )
    )
    path = manifest_mod.write(
        manifest_dir,
        info,
        records,
        turns_by_file,
        seed=args.seed,
        tool_version=TOOL_VERSION,
        target=target,
        dispatch_mode=args.dispatch,
    )
    counts = manifest_mod.summarize(records)
    logger.info("Manifest: %s", path)
    logger.info(
        "Done: %(meetings)d meetings (%(created)d created, %(updated)d updated, %(skipped)d skipped), "
        "%(segments)d segments, %(words)d words, real timings on %(meetings_with_real_timings)d, "
        "synthetic on %(meetings_with_synthetic_timings)d",
        counts,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
