"""Report / re-apply tenant stamps on legacy speaker profiles, speaker collections and
custom-vocabulary terms (issue #1110). The rules and the report are described in
:mod:`app.services.tenant_stamp_backfill`.

Usage (inside a backend container, so it reaches the configured Postgres + OpenSearch)::

    python -m app.scripts.backfill_tenant_stamps
        # dry run: what would be stamped + the rows that need a human decision
    python -m app.scripts.backfill_tenant_stamps --apply --created-before 2026-10-01T00:00:00Z
        # stamp rows created before the cutoff (take it from the deploy time)
    python -m app.scripts.backfill_tenant_stamps --sync-voiceprints
        # write organization_id onto organization profiles' voiceprint documents

Prints a JSON report. Exit status: 0 nothing ambiguous, 4 ambiguous rows reported.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC
from datetime import datetime


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backfill_tenant_stamps", description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the stamps (default: dry run).")
    parser.add_argument(
        "--created-before",
        type=_timestamp,
        help="ISO-8601 cutoff; only rows created before it are stamped. Required with --apply.",
    )
    parser.add_argument(
        "--sync-voiceprints",
        action="store_true",
        help="Copy organization_id onto organization profiles' OpenSearch voiceprint documents.",
    )
    args = parser.parse_args(argv)
    if args.apply and args.created_before is None:
        parser.error("--apply requires --created-before")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("opensearch").setLevel(logging.WARNING)

    from app.db.base import engine
    from app.services import tenant_stamp_backfill as backfill

    with engine.begin() as conn:
        report = backfill.backfill_tenant_stamps(
            conn, apply=args.apply, created_before=args.created_before
        )
        profile_orgs = backfill.org_profile_uuids(conn) if args.sync_voiceprints else {}
    if args.sync_voiceprints:
        report["voiceprints"] = backfill.sync_profile_voiceprint_tenants(profile_orgs)

    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 4 if report["ambiguous_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
