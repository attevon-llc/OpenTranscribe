#!/usr/bin/env python3
"""Remove orphaned test users left behind by pre-savepoint-isolation test runs.

Historical test runs (before backend/tests/conftest.py gained savepoint-based
rollback) committed fixture users to the dev database. Current test runs roll
everything back, so this is a one-time cleanup tool — re-run it any time to
verify the database stays clean.

Safety model:
- DRY-RUN by default: prints what would be deleted. Pass --execute to delete.
- Only matches the exact fixture email patterns used by the test suite.
- Explicit keep-list for real dev accounts (admin@, test@, ldap-*, kc-*, ...).
- Never deletes a user who owns media files — those are reported instead.

Usage (from repo root, inside backend venv):
    python scripts/cleanup-test-users.py            # dry run
    python scripts/cleanup-test-users.py --execute  # actually delete
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import os  # noqa: E402

from dotenv import dotenv_values  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

# Build the DB URL from .env + dev-stack defaults directly — importing
# app.core.config has filesystem side effects (creates data dirs).
_env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")


def _setting(name: str, default: str) -> str:
    return os.environ.get(name) or _env.get(name) or default

# Fixture email patterns from backend/tests/conftest.py and the e2e suite.
# SQL LIKE patterns — '%' wildcard, '_' escaped where it's literal.
ORPHAN_PATTERNS = [
    r"testuser\_%@example.com",
    r"testadmin\_%@example.com",
    r"testsuperadmin\_%@example.com",
    r"otheruser\_%@example.com",
    r"unique\_%@example.com",
    r"newuser\_%@example.com",
    "test-%@example.com",  # test-<uuid>@example.com
]

# Real dev-stack accounts that must never be touched, even if a pattern drifts.
KEEP_EMAILS = {
    "admin@example.com",
    "test@example.com",
    "testuser@example.com",  # legacy manual account — keep unless asked
    "sharetest@example.com",
}
KEEP_PREFIXES = ("ldap-", "kc-", "superdave")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="Actually delete (default: dry run)"
    )
    args = parser.parse_args()

    url = (
        f"postgresql://{_setting('POSTGRES_USER', 'postgres')}:"
        f"{_setting('POSTGRES_PASSWORD', 'postgres')}@"
        f"localhost:{_setting('POSTGRES_TEST_PORT', '5176')}/"
        f"{_setting('POSTGRES_DB', 'opentranscribe')}"
    )
    engine = create_engine(url)
    where = " OR ".join(rf"email LIKE '{p}' ESCAPE '\'" for p in ORPHAN_PATTERNS)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT u.id, u.email,
                       (SELECT COUNT(*) FROM media_file m WHERE m.user_id = u.id) AS files
                FROM "user" u
                WHERE ({where})
                ORDER BY u.email
                """
            )
        ).fetchall()

        candidates, kept, owners = [], [], []
        for row in rows:
            if row.email in KEEP_EMAILS or row.email.startswith(KEEP_PREFIXES):
                kept.append(row.email)
            elif row.files > 0:
                owners.append((row.email, row.files))
            else:
                candidates.append((row.id, row.email))

        print(f"Matched {len(rows)} users:")
        for email in kept:
            print(f"  KEEP    {email} (keep-list)")
        for email, files in owners:
            print(f"  SKIP    {email} (owns {files} media files — review manually)")
        for _id, email in candidates:
            print(f"  {'DELETE' if args.execute else 'WOULD DELETE'}  {email}")

        if not candidates:
            print("Nothing to delete.")
            return 0

        if args.execute:
            ids = [c[0] for c in candidates]
            # The SELECT above already auto-began a transaction on this
            # connection — reuse it and commit, rather than calling begin().
            result = conn.execute(text('DELETE FROM "user" WHERE id = ANY(:ids)'), {"ids": ids})
            conn.commit()
            print(f"Deleted {result.rowcount} orphaned test users.")
        else:
            print(f"\nDry run — {len(candidates)} users would be deleted. "
                  "Re-run with --execute to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
