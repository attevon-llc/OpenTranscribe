"""Run the self-test fixtures against the scanner (audit the auditor)."""

from __future__ import annotations

from session_lifetime_audit.scan import scan_source
from session_lifetime_audit.selftest_cases import SELFTEST_CASES, SELFTEST_CLEAN, SELFTEST_ONCE


def run_selftest(verbose: bool = True) -> list[str]:
    """Return failure descriptions — empty means every detector is alive."""
    failures: list[str] = []

    for category, source in SELFTEST_CASES:
        got = {f.category for f in scan_source(source, 'fixture.py')}
        ok = category in got
        if not ok:
            failures.append(f'{category} did not fire (got {sorted(got) or "nothing"})')
        if verbose:
            mark = '\033[32m✓' if ok else '\033[31m✗'
            print(f'  {mark}\033[0m fires {category}')

    for i, source in enumerate(SELFTEST_CLEAN, start=1):
        found = scan_source(source, 'fixture.py')
        if found:
            detail = ', '.join(f'{f.category}: {f.detail}' for f in found)
            failures.append(f'clean case {i} produced {detail}')
        if verbose:
            mark = '\033[31m✗' if found else '\033[32m✓'
            print(f'  {mark}\033[0m clean case {i} produces no finding')

    for category, source in SELFTEST_ONCE:
        hits = [f for f in scan_source(source, 'fixture.py') if f.category == category]
        if len(hits) != 1:
            failures.append(f'{category} fired {len(hits)} times, expected exactly 1')
        if verbose:
            mark = '\033[32m✓' if len(hits) == 1 else '\033[31m✗'
            print(f'  {mark}\033[0m {category} fires exactly once')

    return failures


def selftest_main() -> int:
    print('\n\033[1maudit-session-lifetime self-test\033[0m\n')
    failures = run_selftest()
    if failures:
        print(f'\n\033[31m{len(failures)} self-test failure(s) — a detector is broken\033[0m')
        for line in failures:
            print(f'  {line}')
        print()
        return 1
    total = len(SELFTEST_CASES) + len(SELFTEST_CLEAN) + len(SELFTEST_ONCE)
    print(f'\n\033[32mall {total} self-test cases pass\033[0m\n')
    return 0
