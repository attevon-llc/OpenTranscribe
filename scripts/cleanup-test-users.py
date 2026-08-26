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
import os  # noqa: E402
import sys
from pathlib import Path

from dotenv import dotenv_values  # noqa: E402
from sqlalchemy import (
    create_engine,  # noqa: E402
    text,  # noqa: E402
)

# Build the DB URL from .env + dev-stack defaults directly — importing
# app.core.config has filesystem side effects (creates data dirs).
_env = dotenv_values(Path(__file__).resolve().parent.parent / '.env')


def _setting(name: str, default: str) -> str:
    return os.environ.get(name) or _env.get(name) or default


# Fixture email patterns from backend/tests/conftest.py and the e2e suite.
# SQL LIKE patterns — '%' wildcard, '_' escaped where it's literal.
#
# Keep in step with the prefixes the e2e suite mints. Every address the suite registers
# must match one of these, or a run killed before its teardown leaves an account no sweep
# can find — which is what happened to `mfa-e2e-<hex>@example.com`: test_mfa.py created one
# per session with no teardown at all and no pattern here matched it.
# `tests/unit/test_e2e_data_hygiene.py` is the gate that stops a NEW unswept prefix
# appearing; this list is the backstop for runs that died mid-flight.
ORPHAN_PATTERNS = [
    r'testuser\_%@example.com',
    r'testadmin\_%@example.com',
    r'testsuperadmin\_%@example.com',
    r'otheruser\_%@example.com',
    r'unique\_%@example.com',
    r'newuser\_%@example.com',
    'test-%@example.com',  # test-<uuid>@example.com
    'reg-e2e-%@example.com',  # e2e registration attempts (test_registration, test_auth_flow)
    'shortname-%@example.com',  # e2e display-name registration test
    'mfa-e2e-%@example.com',  # e2e MFA enrolment user (test_mfa.py session fixture)
    'searchqual-%@example.invalid',  # test_search_quality.py self-seeding corpus owner
]

# Real dev-stack accounts that must never be touched, even if a pattern drifts.
KEEP_EMAILS = {
    'admin@example.com',
    'test@example.com',
    'testuser@example.com',  # legacy manual account — keep unless asked
    'sharetest@example.com',
}
KEEP_PREFIXES = ('ldap-', 'kc-', 'superdave')

# LLM configurations leaked by interrupted test runs. Keyed on the BASE URL, not the
# display name: the URL is what makes a row test infrastructure — these hostnames only
# resolve under test overlays — while names drift ('Mock LLM' vs 'Mock LLM (test)',
# both observed leaked). The damage a leaked row does is worse than a stray user: the
# app sees an ACTIVE provider, auto-dispatches summarization/topic extraction after
# every upload, the connection fails, and the user gets 'AI summary generation failed:
# Network connection failed' toasts on a deployment that (as far as they know) has no
# LLM configured at all. Observed live 2026-08-19: three rows, two pointing at
# mock-llm:5199 (the register_mock_llm_provider fixture — its teardown deletes, but a
# killed run never reaches teardown) and one at llm-test-vllm:8000.
LLM_TEST_URL_PATTERNS = [
    'http://mock-llm:%',  # the mock LLM compose service / test subprocess
    'http://llm-test-vllm:%',  # the vLLM test container overlay
    'http://localhost:5199%',  # the mock bound on the host (subprocess fallback)
    'http://127.0.0.1:5199%',
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Actually delete (default: dry run)')
    args = parser.parse_args()

    url = (
        f'postgresql://{_setting("POSTGRES_USER", "postgres")}:'
        f'{_setting("POSTGRES_PASSWORD", "postgres")}@'
        f'localhost:{_setting("POSTGRES_TEST_PORT", "5176")}/'
        f'{_setting("POSTGRES_DB", "opentranscribe")}'
    )
    engine = create_engine(url)
    where = ' OR '.join(rf"email LIKE '{p}' ESCAPE '\'" for p in ORPHAN_PATTERNS)

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

        print(f'Matched {len(rows)} users:')
        for email in kept:
            print(f'  KEEP    {email} (keep-list)')
        for email, files in owners:
            print(f'  SKIP    {email} (owns {files} media files — review manually)')
        for _id, email in candidates:
            print(f'  {"DELETE" if args.execute else "WOULD DELETE"}  {email}')

        if not candidates:
            print('Nothing to delete.')
            _sweep_leaked_llm_configs(conn, execute=args.execute)
            return 0

        if args.execute:
            ids = [c[0] for c in candidates]
            # The SELECT above already auto-began a transaction on this
            # connection — reuse it and commit, rather than calling begin().
            result = conn.execute(text('DELETE FROM "user" WHERE id = ANY(:ids)'), {'ids': ids})
            conn.commit()
            print(f'Deleted {result.rowcount} orphaned test users.')
        else:
            print(
                f'\nDry run — {len(candidates)} users would be deleted. '
                'Re-run with --execute to apply.'
            )

        _sweep_leaked_llm_configs(conn, execute=args.execute)
    return 0


def _sweep_leaked_llm_configs(conn, *, execute: bool) -> None:
    """Report (and with --execute, delete) LLM configs pointing at test infrastructure.

    Same contract as the user sweep: dry-run by default, and the match is printed row
    by row so nothing is deleted that was never shown.
    """
    where = ' OR '.join(f"base_url LIKE '{p}'" for p in LLM_TEST_URL_PATTERNS)
    rows = conn.execute(
        text(
            f'SELECT id, name, base_url, is_active FROM user_llm_settings WHERE {where} ORDER BY id'
        )
    ).fetchall()

    if not rows:
        print('No leaked LLM test configurations.')
        return

    print(f'\nMatched {len(rows)} LLM configuration(s) pointing at test infrastructure:')
    for row in rows:
        verb = 'DELETE' if execute else 'WOULD DELETE'
        active = ' [ACTIVE — this is what makes uploads toast AI failures]' if row.is_active else ''
        print(f'  {verb}  #{row.id} {row.name!r} -> {row.base_url}{active}')

    if execute:
        ids = [row.id for row in rows]
        result = conn.execute(
            text('DELETE FROM user_llm_settings WHERE id = ANY(:ids)'), {'ids': ids}
        )
        conn.commit()
        print(f'Deleted {result.rowcount} leaked LLM test configuration(s).')
    else:
        print('Re-run with --execute to apply.')


if __name__ == '__main__':
    sys.exit(main())
