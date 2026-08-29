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
- Prints the exact database it is about to act on as the FIRST line of output,
  every run — dry-run or --execute (issue #601).
- A candidate blocked by a leftover child row (tag/task/comment/collection/...)
  is reported and skipped rather than aborting the whole batch (issue #601).

Usage (from repo root, inside backend venv):
    python scripts/cleanup-test-users.py                        # dry run
    python scripts/cleanup-test-users.py --execute-unambiguous  # delete only the
                                                                  # unambiguous tier
    python scripts/cleanup-test-users.py --execute              # delete both tiers

Invoked by ``scripts/cleanup-test-data.py`` (issue #629) as the user/LLM-config plane of a
broader signature-scoped sweep — see that script for the media/collection/tag/watch-source/
speaker-profile/conversation planes, which this script does not touch.
"""

from __future__ import annotations

import argparse
import os  # noqa: E402
import sys
from pathlib import Path
from typing import NamedTuple

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


def _host_setting() -> str:
    """POSTGRES_HOST for the connection this script makes — NOT via ``_setting``.

    ``.env``'s ``POSTGRES_HOST`` names the container-internal compose service
    (``postgres``), which does not resolve from the host process this script runs
    in (its own docstring says "inside backend venv", i.e. on the host). Falling
    back to that value here would silently break every normal invocation. Only an
    explicit environment override — e.g. a test harness pointing this at a
    throwaway container — should change the target; the on-disk ``.env`` never
    should.
    """
    return os.environ.get('POSTGRES_HOST', 'localhost')


# Fixture email patterns from backend/tests/conftest.py and the e2e suite.
# SQL LIKE patterns — '%' wildcard, '_' escaped where it's literal.
#
# Keep in step with the prefixes the e2e suite mints. Every address the suite registers
# must match one of these, or a run killed before its teardown leaves an account no sweep
# can find — which is what happened to `mfa-e2e-<hex>@example.com`: test_mfa.py created one
# per session with no teardown at all and no pattern here matched it.
# `tests/unit/test_e2e_data_hygiene.py` is the gate that stops a NEW unswept prefix
# appearing; this list is the backstop for runs that died mid-flight.
# `tests/unit/test_cleanup_test_users_safety.py::test_every_e2e_registered_prefix_has_an_orphan_pattern`
# is the gate that stops a NEW email-minting prefix appearing here with no match.
#
# Split into two tiers (issue #629): UNAMBIGUOUS patterns carry an `-e2e-`/`-test-`-style
# infix plus a random hex suffix, or an RFC 2606 `.invalid` TLD — no human being ever types
# one of these by hand, so they are safe for `--execute-unambiguous` (no review needed).
# REVIEW patterns (`testuser_%`, `test-%@example.com`, ...) are plausible things a developer
# could hand-create (e.g. `test-foo@example.com`), so they stay behind the full `--execute`
# review gate. `ORPHAN_PATTERNS` is the union, kept so every existing caller/test that reads
# it unchanged keeps working.
ORPHAN_PATTERNS_UNAMBIGUOUS = [
    'reg-e2e-%@example.com',  # e2e registration attempts (test_registration, test_auth_flow)
    'shortname-%@example.com',  # e2e display-name registration test
    'mfa-e2e-%@example.com',  # e2e MFA enrolment user (test_mfa.py session fixture)
    'searchqual-%@example.invalid',  # test_search_quality.py self-seeding corpus owner
    'share-e2e-%@example.com',  # e2e second-user fixture (conftest.SECOND_USER_PREFIX)
]

ORPHAN_PATTERNS_REVIEW = [
    r'testuser\_%@example.com',
    r'testadmin\_%@example.com',
    r'testsuperadmin\_%@example.com',
    r'otheruser\_%@example.com',
    r'unique\_%@example.com',
    r'newuser\_%@example.com',
    'test-%@example.com',  # test-<uuid>@example.com
]

ORPHAN_PATTERNS = [*ORPHAN_PATTERNS_UNAMBIGUOUS, *ORPHAN_PATTERNS_REVIEW]

# Real dev-stack accounts that must never be touched, even if a pattern drifts.
KEEP_EMAILS = {
    'admin@example.com',
    'test@example.com',
    'testuser@example.com',  # legacy manual account — keep unless asked
    'pkiadmin@example.com',  # PKI E2E admin-cert identity (issue #593) — JIT-provisioned
    # by test_pki.py's admin_cert_context fixture, same treatment as testuser@example.com.
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


class UserRow(NamedTuple):
    """One matched candidate row — deliberately DB-agnostic so ``classify`` is pure."""

    id: int
    email: str
    files: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Actually delete (default: dry run) — unambiguous AND review-tier candidates',
    )
    parser.add_argument(
        '--execute-unambiguous',
        action='store_true',
        help=(
            'Actually delete, but ONLY the unambiguous-tier candidates '
            '(ORPHAN_PATTERNS_UNAMBIGUOUS) — review-tier candidates are still only reported'
        ),
    )
    return parser


def classify(
    rows,
) -> tuple[list[str], list[tuple[str, int]], list[tuple[int, str]]]:
    """Partition matched rows into keep / owner / candidate buckets. No DB access, no I/O.

    ``rows`` is any iterable of objects exposing ``.id``, ``.email`` and ``.files`` —
    a SQLAlchemy ``Row`` from the live query, or a plain :class:`UserRow` in tests.
    """
    kept: list[str] = []
    owners: list[tuple[str, int]] = []
    candidates: list[tuple[int, str]] = []
    for row in rows:
        if row.email in KEEP_EMAILS or row.email.startswith(KEEP_PREFIXES):
            kept.append(row.email)
        elif row.files > 0:
            owners.append((row.email, row.files))
        else:
            candidates.append((row.id, row.email))
    return kept, owners, candidates


#: Every FK into "user" the database will not sweep on its own (``confdeltype <>
#: 'c'``), derived at query time rather than hardcoded — the same lesson
#: backend/tests/unit/test_user_deletion_fk_coverage.py encodes about the app's own
#: two deletion paths. Hardcoding this list is exactly how it would drift the moment
#: a migration adds a ninth one.
_BLOCKING_FK_SQL = """
    SELECT c.conrelid::regclass::text AS child, a.attname AS col
    FROM pg_constraint c
    JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    WHERE c.contype = 'f'
      AND c.confrelid = CAST(:parent AS regclass)
      AND c.confdeltype <> 'c'
"""


def _blocking_fk_children(conn) -> list[tuple[str, str]]:
    """``[(child_table, column), ...]`` for every non-CASCADE FK into ``"user"``."""
    rows = conn.execute(text(_BLOCKING_FK_SQL), {'parent': '"user"'}).all()
    return [(row.child.strip('"'), row.col) for row in rows]


def blocked_by_foreign_keys(conn, ids_by_email: dict[int, str]) -> dict[str, str]:
    """``{email: "table.column"}`` for every candidate a leftover child row blocks.

    Called in BOTH dry-run and --execute so the dry-run report never promises a
    deletion the database will refuse (issue #601, Bug C). A candidate present here
    must never be attempted — deleting it would raise ``IntegrityError`` and, before
    this function existed, aborted the ENTIRE batch (issue #601, Bug B): the delete
    was one statement for every candidate, so one blocked user prevented every other
    legitimate orphan from being removed too.
    """
    if not ids_by_email:
        return {}
    ids = list(ids_by_email)
    blocked: dict[str, str] = {}
    for child, col in _blocking_fk_children(conn):
        matches = (
            conn.execute(
                text(f'SELECT DISTINCT "{col}" FROM "{child}" WHERE "{col}" = ANY(:ids)'),
                {'ids': ids},
            )
            .scalars()
            .all()
        )
        for uid in matches:
            email = ids_by_email.get(uid)
            if email and email not in blocked:
                blocked[email] = f'{child}.{col}'
    return blocked


def _delete_users(
    conn, deletable: list[tuple[int, str]], *, execute: bool
) -> tuple[list[str], list[str]]:
    """Delete each already-cleared candidate inside its own savepoint.

    Pre-filtering on :func:`blocked_by_foreign_keys` should mean every row here
    deletes cleanly, but the savepoint is defense in depth: an unforeseen
    constraint this script's FK derivation did not anticipate now costs one row,
    never the whole batch. Safe to call unconditionally — a dry run passes
    ``execute=False`` and the DELETE never runs.
    """
    removed: list[str] = []
    failed: list[str] = []
    if execute:
        for uid, email in deletable:
            try:
                with conn.begin_nested():
                    conn.execute(text('DELETE FROM "user" WHERE id = :id'), {'id': uid})
            except Exception as exc:
                failed.append(email)
                print(f'  ERROR   {email}: {exc}')
            else:
                removed.append(email)
        conn.commit()
    return removed, failed


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    db_user = _setting('POSTGRES_USER', 'postgres')
    db_password = _setting('POSTGRES_PASSWORD', 'postgres')
    db_host = _host_setting()
    db_port = _setting('POSTGRES_PORT', '5176')
    db_name = _setting('POSTGRES_DB', 'opentranscribe')

    # First line of ALL output, dry-run or --execute: what this run is about to act
    # on. Before this, a --execute could silently resolve to the wrong database
    # (see POSTGRES_PORT below) with no way to notice from the output alone.
    print(f'Target: {db_user}@{db_host}:{db_port}/{db_name}')

    url = f'postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'
    engine = create_engine(url)
    where = ' OR '.join(rf"email LIKE '{p}' ESCAPE '\'" for p in ORPHAN_PATTERNS)
    where_unambiguous = ' OR '.join(
        rf"email LIKE '{p}' ESCAPE '\'" for p in ORPHAN_PATTERNS_UNAMBIGUOUS
    )

    # --execute-unambiguous restricts DELETION to the unambiguous tier; --execute deletes
    # both tiers. Both are "execute" for the purposes of the LLM-config sweep (Tier A) and
    # for the DELETE/WOULD DELETE verb on rows that end up in `deletable`.
    effective_execute = args.execute or args.execute_unambiguous

    exit_code = 0
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

        kept, owners, candidates = classify(rows)
        ids_by_email = dict(candidates)
        blocked = blocked_by_foreign_keys(conn, ids_by_email) if candidates else {}
        deletable = [(uid, email) for uid, email in candidates if email not in blocked]

        if args.execute_unambiguous and not args.execute and deletable:
            unambiguous_emails = set(
                conn.execute(
                    text(
                        f'SELECT email FROM "user" WHERE email = ANY(:emails) '
                        f'AND ({where_unambiguous})'
                    ),
                    {'emails': [email for _uid, email in deletable]},
                )
                .scalars()
                .all()
            )
            deletable = [(uid, email) for uid, email in deletable if email in unambiguous_emails]

        deletable_ids = {uid for uid, _email in deletable}

        print(f'Matched {len(rows)} users:')
        for email in kept:
            print(f'  KEEP    {email} (keep-list)')
        for email, files in owners:
            print(f'  SKIP    {email} (owns {files} media files — review manually)')
        for uid, email in candidates:
            if email in blocked:
                print(f'  BLOCKED {email} ({blocked[email]})')
            elif uid in deletable_ids:
                verb = 'DELETE' if effective_execute else 'WOULD DELETE'
                print(f'  {verb}  {email}')
            else:
                # Review-tier candidate not selected by --execute-unambiguous alone.
                print(f'  WOULD DELETE  {email}')

        removed, failed = _delete_users(conn, deletable, execute=effective_execute)

        if not candidates:
            print('Nothing to delete.')
        elif effective_execute:
            print(f'Deleted {len(removed)} orphaned test users.')
            if blocked or failed:
                # A partial sweep must not read as success — run-integration-tests.sh's
                # dry-run caller already tolerates a non-zero exit here (`|| true`).
                exit_code = 1
        else:
            note = f', {len(blocked)} blocked' if blocked else ''
            print(
                f'\nDry run — {len(deletable)} users would be deleted{note}. '
                'Re-run with --execute (or --execute-unambiguous) to apply.'
            )

        _sweep_leaked_llm_configs(conn, execute=effective_execute)
    return exit_code


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
