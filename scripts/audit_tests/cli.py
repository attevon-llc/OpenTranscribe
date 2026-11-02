"""Command-line entry: scan a tree, apply the allowlist, report."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from audit_tests.allowlist import (
    ALLOWLIST_NAME,
    BACKLOG_PREFIX,
    AllowlistReasonError,
    apply_allowlist,
    load_allowlist,
)
from audit_tests.findings import CATEGORIES, Finding
from audit_tests.scan import scan_file
from audit_tests.selftest import run_selftest, selftest_main

# ----------------------------------------------------------------------------------- main


def main(description: str) -> int:
    ap = argparse.ArgumentParser(
        description=description, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('root', type=Path, nargs='?', help='test tree to scan (e.g. backend/tests)')
    ap.add_argument('--category', choices=CATEGORIES, help='limit to one detector')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--no-e2e', action='store_true', help='skip tests/e2e (scanned by default)')
    ap.add_argument('--list', action='store_true', help='print every finding, not just counts')
    ap.add_argument(
        '--selftest', action='store_true', help='audit the auditor against in-memory fixtures'
    )
    args = ap.parse_args()

    if args.selftest:
        return selftest_main()
    if args.root is None:
        ap.error('root is required unless --selftest is given')
    if not args.root.is_dir():
        print(f'error: {args.root} is not a directory', file=sys.stderr)
        return 2

    # A broken detector reports zero findings, so never let the tree scan speak without it.
    selftest_failures = run_selftest(verbose=False)

    findings: list[Finding] = []
    for path in sorted(args.root.rglob('*.py')):
        if args.no_e2e and 'e2e' in path.parts:
            continue
        findings.extend(scan_file(path, args.root))

    if args.category:
        findings = [f for f in findings if f.category == args.category]

    try:
        allowed = load_allowlist(args.root)
    except AllowlistReasonError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    unallowed, backlog, accepted, all_stale = apply_allowlist(findings, allowed)

    # An allowlist entry with no finding left to cover is an entry nobody will ever delete.
    # Only meaningful on a FULL scan — a filtered one has not looked at the other categories.
    full_scan = not args.category and not args.no_e2e
    stale = all_stale if full_scan else []

    if args.json:
        print(
            json.dumps(
                {
                    'total': len(findings),
                    'accepted': accepted,
                    'backlog': len(backlog),
                    'unallowlisted': len(unallowed),
                    'stale_allowlist_entries': stale,
                    'selftest_failures': selftest_failures,
                    'by_category': dict(Counter(f.category for f in unallowed)),
                    'backlog_by_category': dict(Counter(f.category for f in backlog)),
                    'findings': [f.__dict__ for f in unallowed],
                },
                indent=2,
            )
        )
        return 1 if (unallowed or stale or selftest_failures) else 0

    counts = Counter(f.category for f in findings)
    backlog_counts = Counter(f.category for f in backlog)
    print(
        f'\n\033[1m{args.root}\033[0m — {len(findings)} findings: '
        f'{len(unallowed)} open, {len(backlog)} backlog, {accepted} accepted\n'
    )
    for category in CATEGORIES:
        hits = [f for f in unallowed if f.category == category]
        total = counts.get(category, 0)
        colour = '\033[31m' if hits else '\033[32m'
        deferred = backlog_counts.get(category, 0)
        suffix = f', {deferred} backlog' if deferred else ''
        print(f'  {colour}{category:22s}\033[0m {len(hits):4d} open  ({total} total{suffix})')
        if args.list or len(hits) <= 5:
            for f in hits[: (None if args.list else 5)]:
                print(f'      {f.path}:{f.line} {f.test} — {f.detail}')
        elif hits:
            print(f'      … {len(hits)} findings (--list)')

    if selftest_failures:
        print(f'\n\033[31mSELF-TEST BROKEN — {len(selftest_failures)} detector(s) dead:\033[0m')
        for line in selftest_failures:
            print(f'  {line}')
        print('  The counts above are not trustworthy. Run --selftest.\n')
    if stale:
        print(f'\n\033[31m{len(stale)} allowlist key(s) hold more entries than findings:\033[0m')
        for key in stale[:20]:
            print(f'  {key}')
        if len(stale) > 20:
            print(f'  … {len(stale) - 20} more')
        print(f'  Delete the surplus lines from {args.root / ALLOWLIST_NAME}. One line buys')
        print('  one finding: a stale exemption is a blanket nobody reviews, and a surplus one')
        print('  keeps covering the assertions you did NOT fix.\n')
    if backlog:
        print(
            f'\033[1;33m{len(backlog)} finding(s) are DEFERRED WORK, not accepted patterns.\033[0m'
        )
        print(
            f'  They carry a `{BACKLOG_PREFIX}` reason in {args.root / ALLOWLIST_NAME}. '
            'This gate is green\n  because nothing NEW landed — not because the tree is clean.'
        )
    if unallowed:
        print(f'\n\033[31m{len(unallowed)} findings need a fix or an allowlist entry\033[0m')
        print(
            f'  allowlist: {args.root / ALLOWLIST_NAME}'
            '  (one "<file>::<test>::<category>  # reason" per line)\n'
        )
    if unallowed or stale or selftest_failures:
        return 1
    print('\n\033[32mno un-allowlisted findings\033[0m\n')
    return 0
