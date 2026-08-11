#!/usr/bin/env python3
"""Compare the live database schema against the SQLAlchemy models.

WHY THIS IS A REPORT, NOT A GATE (yet)
======================================

Nothing in this repo compared ``Base.metadata`` to the migrated schema, so a
model change without a matching migration surfaced only at runtime. The obvious
fix is a pytest that asserts ``compare_metadata()`` returns an empty list.

A recon run against the dev database returned **392 diffs**, so that assertion
would have needed a 392-entry allowlist — which is not a check, it is a
changelog. Before writing one, the diffs were categorised, and almost all of them
come from two systemic causes that are cosmetic:

1. **Index naming.** 158 ``remove_index`` + 135 ``add_index``. The migrations are
   hand-written raw SQL (see backend/alembic/CLAUDE.md — 56 of 59 revisions use
   ``op.execute``) and name their indexes ``idx_<table>_<col>``. The models
   declare ``index=True``, and SQLAlchemy's implicit name is ``ix_<table>_<col>``.
   Both indexes exist and cover the same column; only the *name* differs, so
   autogenerate sees one to drop and one to create. Fixing this means either
   renaming every index in a migration or spelling out every index name in the
   models. Neither is free, and ``backend/app/db/base.py`` deliberately has no
   naming convention because adding one renames existing constraints.

2. **Orphan tables.** ``upload_session``, ``speaker_audio_clip`` and
   ``user_certificate_preferences`` exist in the database with no model and no
   reference anywhere under ``app/`` — migration-created leftovers from removed
   features.

So the honest position: the *count* is not currently meaningful, but the
*categories* are. This script reports them separately so the signal categories
(a missing table, a missing column, a type change) are readable instead of being
buried under 293 index renames.

The always-on protections that came out of the same work and DO gate:
  * tests/unit/test_model_registration.py — every model reaches Base.metadata
    (this found system_settings missing from app/models/__init__.py)
  * tests/unit/test_alembic_chain.py — single head, reachability, id conventions

Usage:
    ./scripts/check-schema-drift.py                 # human report
    ./scripts/check-schema-drift.py --json
    ./scripts/check-schema-drift.py --fail-on tables,columns   # gate categories

Exit codes: 0 clean (for the selected categories), 1 drift found, 2 misuse,
3 database unreachable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"

# Categories worth gating on: each one means the application will hit a runtime
# error (a query against a column or table that is not there).
SIGNAL_CATEGORIES = {
    "add_table": "tables",
    "remove_table": "tables",
    "add_column": "columns",
    "remove_column": "columns",
    "modify_type": "types",
    "modify_nullable": "types",
}
# Categories dominated by the naming mismatch described above.
COSMETIC_CATEGORIES = {
    "add_index": "indexes",
    "remove_index": "indexes",
    "add_constraint": "constraints",
    "remove_constraint": "constraints",
    "add_fk": "constraints",
    "remove_fk": "constraints",
}


def _bootstrap_env() -> None:
    """Point the app at the dev stack's Postgres without importing conftest."""
    os.environ.setdefault("POSTGRES_HOST", "localhost")
    os.environ.setdefault("POSTGRES_PORT", "5176")
    os.environ.setdefault("SKIP_S3", "True")
    os.environ.setdefault("SKIP_OPENSEARCH", "True")
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "ot-schema-drift"
    scratch.mkdir(parents=True, exist_ok=True)
    for key in ("DATA_DIR", "MODELS_DIR", "TEMP_DIR"):
        os.environ.setdefault(key, str(scratch))

    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    for key, value in (dotenv_values(REPO_ROOT / ".env") or {}).items():
        if key.startswith("POSTGRES_") and key not in ("POSTGRES_HOST", "POSTGRES_PORT"):
            os.environ.setdefault(key, value or "")


def collect_diffs() -> list:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db.base import Base, engine

    import app.models  # noqa: F401  registers every model on Base.metadata

    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                # Server defaults are the single largest false-positive source
                # (now() vs CURRENT_TIMESTAMP, '{}'::jsonb vs '{}'). Off until the
                # signal categories are clean.
                "compare_server_default": False,
                "include_object": lambda obj, name, type_, reflected, compare_to: (
                    name != "alembic_version"
                ),
            },
        )
        return compare_metadata(context, Base.metadata)


def classify(diffs: list) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for diff in diffs:
        # Nested diffs arrive as a list of tuples (column-level modifications).
        entries = diff if isinstance(diff, list) else [diff]
        for entry in entries:
            if not isinstance(entry, tuple) or not entry:
                grouped["other"].append(str(entry)[:200])
                continue
            kind = str(entry[0])
            group = SIGNAL_CATEGORIES.get(kind) or COSMETIC_CATEGORIES.get(kind) or "other"
            grouped[f"{group}:{kind}"].append(str(entry)[:200])
    return dict(grouped)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--fail-on",
        default="",
        help="comma-separated categories to exit non-zero on (tables,columns,types,indexes,constraints)",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    _bootstrap_env()

    try:
        diffs = collect_diffs()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not compare schema: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Is the dev stack up? `./opentr.sh start dev`", file=sys.stderr)
        return 3

    grouped = classify(diffs)
    fail_on = {c.strip() for c in args.fail_on.split(",") if c.strip()}

    totals: dict[str, int] = defaultdict(int)
    for key, items in grouped.items():
        totals[key.split(":")[0]] += len(items)

    offending = sorted(fail_on & set(totals))

    if args.json:
        print(
            json.dumps(
                {
                    "stage": "schema-drift",
                    "status": "fail" if offending else "pass",
                    "total_diffs": sum(totals.values()),
                    "by_category": dict(totals),
                    "by_kind": {k: len(v) for k, v in sorted(grouped.items())},
                    "gated_categories": sorted(fail_on),
                    "violations": offending,
                },
                indent=2,
            )
        )
        return 1 if offending else 0

    print(f"Model-vs-schema diffs: {sum(totals.values())}\n")
    for category in sorted(totals):
        marker = "  <-- GATED" if category in fail_on else ""
        print(f"  {category:<14} {totals[category]:>4}{marker}")
    print()
    for key in sorted(grouped):
        category = key.split(":")[0]
        if category in {"tables", "columns", "types", "other"}:
            print(f"[{key}]")
            for item in grouped[key][:15]:
                print(f"    {item}")
            if len(grouped[key]) > 15:
                print(f"    ... and {len(grouped[key]) - 15} more")
    if offending:
        print(f"\nFAIL: drift in gated categories: {offending}")
        return 1
    print("\nOK for the gated categories:", sorted(fail_on) or "(none gated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
