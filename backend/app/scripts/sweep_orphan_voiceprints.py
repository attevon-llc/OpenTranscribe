"""Count / remove voiceprint documents whose speaker or profile row no longer exists.

Usage (inside a backend container, so it reaches the configured Postgres + OpenSearch)::

    python -m app.scripts.sweep_orphan_voiceprints            # dry run — counts only
    python -m app.scripts.sweep_orphan_voiceprints --apply    # delete the orphans
    python -m app.scripts.sweep_orphan_voiceprints --apply --force
        # only if the dry run reported refused=empty_database AND Postgres is verified

Prints the JSON report from :func:`app.services.voiceprint_orphan_sweep.sweep_orphan_voiceprints`.
Exit status: 0 clean / dry run, 2 refused, 3 orphans survived an --apply.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sweep_orphan_voiceprints", description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Delete the orphans (default: dry run)."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed even though the database holds no speaker or profile rows.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("opensearch").setLevel(logging.WARNING)

    from app.services.voiceprint_orphan_sweep import sweep_orphan_voiceprints

    report = sweep_orphan_voiceprints(dry_run=not args.apply, force=args.force)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["refused"]:
        return 2
    if report["surviving_orphans"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
