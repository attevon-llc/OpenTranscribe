"""Re-verify the voice-made speaker stamps the 0.6.0 upgrade (v449) wrote (issue #1334).

Usage (inside a backend container, so it reaches the configured Postgres)::

    python -m app.scripts.reverify_identity_sources                    # dry run: report only
    python -m app.scripts.reverify_identity_sources --organization-id 3
    python -m app.scripts.reverify_identity_sources --apply            # clear unfounded stamps
    python -m app.scripts.reverify_identity_sources --apply --include-evidenced
        # clear EVERY v449 stamp: keep every pre-0.6.0 name as a person's

Prints the JSON report from
:func:`app.services.privacy.identity_reverify.reverify_identity_sources` and writes a
``privacy.identity_source.reverify`` audit event, dry run or not. ``--include-evidenced`` without
``--apply`` is refused: a dry run already lists both kinds of stamp.

Exit status: 0 done, 2 misuse, 3 the stamps were cleared but a search reindex of some affected
files could not be queued (listed in ``reindex_failed_file_uuids``): while voiceprints are off,
their index still hides the name until they are re-indexed (a library reindex from the admin
search settings does it).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reverify_identity_sources", description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Clear stamps (default: dry run).")
    parser.add_argument(
        "--include-evidenced",
        action="store_true",
        help="With --apply, clear every v449 stamp, also those whose evidence still holds.",
    )
    parser.add_argument("--organization-id", type=int, help="Only this organization's speakers.")
    parser.add_argument("--user-id", type=int, help="Only this user's speakers.")
    args = parser.parse_args(argv)
    if args.include_evidenced and not args.apply:
        parser.error("--include-evidenced only applies with --apply")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.db.base import SessionLocal
    from app.services.privacy.identity_reverify import ReverifyScope
    from app.services.privacy.identity_reverify import reverify_identity_sources

    db = SessionLocal()
    try:
        report = reverify_identity_sources(
            db,
            scope=ReverifyScope(organization_id=args.organization_id, user_id=args.user_id),
            apply=args.apply,
            include_evidenced=args.include_evidenced,
        )
    finally:
        db.close()
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 3 if report.reindex_queue_failed else 0


if __name__ == "__main__":
    sys.exit(main())
