"""Command-line entry: scan a tree, apply the allowlist, report."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from session_lifetime_audit.allowlist import (
    ALLOWLIST_PATH,
    BACKLOG_PREFIX,
    apply_allowlist,
    load_allowlist,
)
from session_lifetime_audit.findings import CATEGORIES, Finding
from session_lifetime_audit.scan import scan_file
from session_lifetime_audit.selftest import run_selftest, selftest_main


def main(description: str) -> int:
    ap = argparse.ArgumentParser(
        description=description, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('root', type=Path, nargs='?', help='source tree to scan (e.g. backend/app)')
    ap.add_argument('--category', choices=CATEGORIES, help='limit to one detector')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
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
        findings.extend(scan_file(path, args.root))

    if args.category:
        findings = [f for f in findings if f.category == args.category]

    allowed = load_allowlist()
    unallowed, backlog, accepted, all_stale, unreasoned = apply_allowlist(findings, allowed)
    stale = all_stale if not args.category else []

    if args.json:
        print(
            json.dumps(
                {
                    'total': len(findings),
                    'accepted': accepted,
                    'backlog': len(backlog),
                    'unallowlisted': len(unallowed),
                    'stale_allowlist_entries': stale,
                    'unreasoned_allowlist_entries': unreasoned,
                    'selftest_failures': selftest_failures,
                    'by_category': dict(Counter(f.category for f in unallowed)),
                    'backlog_by_category': dict(Counter(f.category for f in backlog)),
                    'findings': [f.__dict__ for f in unallowed],
                },
                indent=2,
            )
        )
        return 1 if (unallowed or stale or unreasoned or selftest_failures) else 0

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
        print(f'  {colour}{category:26s}\033[0m {len(hits):4d} open  ({total} total{suffix})')
        if args.list or len(hits) <= 5:
            for f in hits[: (None if args.list else 5)]:
                print(f'      {f.path}:{f.line} {f.scope} — {f.detail}')
        elif hits:
            print(f'      … {len(hits)} findings (--list)')

    if selftest_failures:
        print(f'\n\033[31mSELF-TEST BROKEN — {len(selftest_failures)} detector(s) dead:\033[0m')
        for line in selftest_failures:
            print(f'  {line}')
        print('  The counts above are not trustworthy. Run --selftest.\n')
    if unreasoned:
        print(f'\n\033[31m{len(unreasoned)} allowlist entr(y/ies) carry NO reason:\033[0m')
        for key in unreasoned[:20]:
            print(f'  {key}')
        print(
            '  Every exemption needs a written reason — one nobody had to justify is one\n'
            '  nobody will ever revisit.\n'
        )
    if stale:
        print(f'\n\033[31m{len(stale)} allowlist key(s) hold more entries than findings:\033[0m')
        for key in stale[:20]:
            print(f'  {key}')
        if len(stale) > 20:
            print(f'  … {len(stale) - 20} more')
        print(f'  Delete the surplus lines from {ALLOWLIST_PATH}. One line buys one finding:')
        print('  a stale exemption is a blanket nobody reviews, and a surplus one keeps')
        print('  covering the calls you did NOT move out of the transaction.\n')
    if backlog:
        print(
            f'\033[1;33m{len(backlog)} finding(s) are DEFERRED WORK, not accepted patterns.\033[0m'
        )
        print(
            f'  They carry a `{BACKLOG_PREFIX}` reason in {ALLOWLIST_PATH}. This gate is\n'
            '  green because nothing NEW landed — not because the tree is clean.'
        )
    if unallowed:
        print(f'\n\033[31m{len(unallowed)} findings need a fix or an allowlist entry\033[0m')
        print(f'  allowlist: {ALLOWLIST_PATH}')
        print('  (one "<file>::<scope>::<category>  # reason" per line)')
        print('  The fix pattern is in backend/app/tasks/CLAUDE.md: a short read session')
        print('  returning PLAIN DATA, the slow work with NO session, then a short write.\n')
    if unallowed or stale or unreasoned or selftest_failures:
        return 1
    print('\n\033[32mno un-allowlisted findings\033[0m\n')
    return 0
