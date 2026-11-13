"""Argument parsing, the record sweep and ``main``."""

from __future__ import annotations

import argparse
import json as json_module
import subprocess
import sys
from typing import Any

from sqlalchemy import create_engine

import cleanup_test_data
from cleanup_test_data.api import ApiSession
from cleanup_test_data.cutoff import (
    _TESTRUNS_DIR,
    apply_age_floor,
    live_marker_start_times,
    resolve_cutoff,
)
from cleanup_test_data.finders import (
    Candidate,
    find_injected_test_media,
    find_media_by_filename,
    find_media_by_tier_a_owner,
    find_named_resources,
)
from cleanup_test_data.owners import (
    _delete_candidates,
    _delete_owners,
    find_deletable_test_owners,
    owner_patterns,
    owners_to_delete,
)
from cleanup_test_data.planes import PLANES, _sweep_store_planes
from cleanup_test_data.settings import (
    _CLEANUP_USERS_SCRIPT,
    _backend_url,
    _engine_url,
    _host_setting,
    _load_cleanup_users_module,
    _setting,
)


def _parse_planes(value: str) -> tuple[str, ...]:
    planes = tuple(p.strip() for p in value.split(',') if p.strip())
    unknown = [p for p in planes if p not in PLANES]
    if unknown or not planes:
        raise argparse.ArgumentTypeError(
            f'unknown plane(s) {unknown or value!r}; choose from {",".join(PLANES)}'
        )
    return planes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=cleanup_test_data.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--execute-unambiguous', action='store_true', help='Delete Tier A candidates only'
    )
    parser.add_argument(
        '--execute', action='store_true', help='Delete Tier A + Tier B (review) candidates'
    )
    parser.add_argument('--json', action='store_true', help='Machine-readable aggregate report')
    parser.add_argument(
        '--min-age-minutes',
        type=int,
        default=0,
        help='Never touch anything younger than this (on top of the liveness cutoff)',
    )
    parser.add_argument(
        '--planes',
        type=_parse_planes,
        default=PLANES,
        help=f'Comma-separated subset of {",".join(PLANES)} (default: all)',
    )
    return parser


def _sweep_records(args: argparse.Namespace, cutoff: int, report: dict[str, Any]) -> int:
    effective_execute = args.execute or args.execute_unambiguous
    backend_url = _backend_url()
    cleanup_users = _load_cleanup_users_module()
    engine = create_engine(_engine_url())
    exit_code = 0
    with engine.connect() as conn:
        media_candidates = find_media_by_filename(conn, cutoff)
        media_candidates += find_injected_test_media(conn, cutoff)
        media_candidates += find_media_by_tier_a_owner(
            conn, cutoff, cleanup_users.ORPHAN_PATTERNS_UNAMBIGUOUS
        )
        # De-dupe: a Tier-A-owned file could also match a filename spec.
        seen_uuids: set[str] = set()
        deduped_media: list[Candidate] = []
        for cand in media_candidates:
            if cand.uuid not in seen_uuids:
                seen_uuids.add(cand.uuid)
                deduped_media.append(cand)
        media_candidates = deduped_media

        named = find_named_resources(conn, cutoff)
        deletable_owners = find_deletable_test_owners(
            conn, cutoff, owner_patterns(cleanup_users, include_tier_b=bool(args.execute))
        )

    all_candidates: dict[str, list[Candidate]] = {'media_file': media_candidates, **named}
    owners = owners_to_delete(all_candidates, deletable_owners)

    api = ApiSession(backend_url)
    api_available = api.login()
    if not api_available:
        print('  WARN  API plane unavailable — skipping media/collection/tag/watch-source/')
        print('        speaker-profile/conversation deletion this run (DB-only planes still run)')
        exit_code = 1

    owner_outcome: dict[str, bool] = {}
    print(f'\nregistered test owners: {len(owners)} account(s) to delete with their rows')
    for email in owners:
        verb = 'DELETE' if (effective_execute and api_available) else 'WOULD DELETE'
        print(f'  {verb}  user {email}  (rows the admin cannot delete individually)')
    report['resources']['test_owners'] = {'count': len(owners), 'deleted': 0}
    # Same guard as the per-record deletes below (test_cleanup_test_data_safety.py's AST scan).
    if effective_execute and api_available and owners:
        owner_outcome = _delete_owners(api, owners)
        report['resources']['test_owners']['deleted'] = sum(owner_outcome.values())
        if not all(owner_outcome.values()):
            exit_code = 1
        print(f'  Deleted {sum(owner_outcome.values())}/{len(owners)} owner account(s)')

    for resource, candidates in all_candidates.items():
        print(f'\n{resource}: {len(candidates)} Tier A candidate(s)')
        report['resources'][resource] = {'count': len(candidates), 'deleted': 0}
        for cand in candidates:
            verb = 'DELETE' if (effective_execute and api_available) else 'WOULD DELETE'
            via = ' via owner' if cand.owner_email in owners else ''
            print(f'  {verb}{via}  {cand.label}  (owner={cand.owner_email})')

        # Every actual API delete call lives behind THIS guard, and only this guard —
        # see test_cleanup_test_data_safety.py's AST scan, which fails a build the
        # moment a delete call appears anywhere it cannot reach.
        if effective_execute and api_available:
            deleted, resource_failed = _delete_candidates(api, resource, candidates, owner_outcome)
            if resource_failed:
                exit_code = 1
            report['resources'][resource]['deleted'] = deleted
            print(f'  Deleted {deleted}/{len(candidates)}')

    api.logout()

    # User/LLM-config plane: delegate to cleanup-test-users.py (one implementation, no
    # second copy of ORPHAN_PATTERNS logic). Run LAST — media/collection/tag/watch/
    # profile/conversation deletion above clears the `files > 0` block that otherwise
    # keeps a corpus-owning user (e.g. searchqual-*) permanently un-sweepable.
    subprocess_flag = '--execute-unambiguous' if args.execute_unambiguous else None
    if args.execute:
        subprocess_flag = '--execute'

    cmd = [sys.executable, str(_CLEANUP_USERS_SCRIPT)]
    if subprocess_flag:
        cmd.append(subprocess_flag)
    print(f'\n--- {" ".join(cmd)} ---', flush=True)
    result = subprocess.run(cmd, capture_output=False, check=False)
    if result.returncode not in (0,):
        exit_code = 1

    return exit_code


def main() -> int:
    args = build_parser().parse_args()

    backend_url = _backend_url()
    db_user = _setting('POSTGRES_USER', 'postgres')
    db_host = _host_setting()
    db_port = _setting('POSTGRES_PORT', '')
    db_name = _setting('POSTGRES_DB', 'opentranscribe')
    print(f'Target: {db_user}@{db_host}:{db_port}/{db_name} + API {backend_url}')
    print(f'Planes: {",".join(args.planes)}')

    live_starts = live_marker_start_times(_TESTRUNS_DIR)
    cutoff = apply_age_floor(resolve_cutoff(live_starts), args.min_age_minutes)
    print(
        f'Cutoff: rows created before {cutoff} ({len(live_starts)} concurrently live '
        f'run marker(s) found in {_TESTRUNS_DIR}; age floor {args.min_age_minutes} min)'
    )

    report: dict[str, Any] = {'cutoff': cutoff, 'planes': list(args.planes), 'resources': {}}
    exit_code = 0
    if 'records' in args.planes:
        exit_code = max(exit_code, _sweep_records(args, cutoff, report))
    if any(p != 'records' for p in args.planes):
        exit_code = max(exit_code, _sweep_store_planes(args, cutoff, report))

    if args.json:
        print('\n--- JSON report ---')
        print(json_module.dumps(report, indent=2))

    return exit_code
