"""Self-test runner: audits the auditor against in-memory fixtures."""

from __future__ import annotations

from collections.abc import Callable

from audit_tests.allowlist import AllowlistReasonError, parse_allowlist_text
from audit_tests.scan import scan_source
from audit_tests.selftest_cases import (
    SELFTEST_CASES,
    SELFTEST_CLEAN,
    SELFTEST_ONCE,
    SELFTEST_PATH_CASES,
    SELFTEST_PATH_CLEAN,
)


def _allowlist_rejects(text: str) -> bool:
    """True iff parsing ``text`` raises :class:`AllowlistReasonError`."""
    try:
        parse_allowlist_text(text)
    except AllowlistReasonError:
        return True
    return False


#: ``(label, check)`` pairs for the allowlist RAILS themselves (issue #826) — not the
#: detectors. A detector that stops matching is invisible; so is a rail that stops rejecting a
#: reason-less entry, which is exactly what shipped: an empty or placeholder reason was
#: silently substituted with `'no reason given'` instead of failing the run. Each check is a
#: predicate so `run_selftest` can report it the same way as a detector case.
ALLOWLIST_SELFTEST_CASES: tuple[tuple[str, Callable[[], bool]], ...] = (
    (
        'a reason-less entry is REJECTED, not defaulted to a placeholder',
        lambda: _allowlist_rejects('a.py::test_a::weak-only'),
    ),
    (
        'an empty reason after "#" is REJECTED',
        lambda: _allowlist_rejects('a.py::test_a::weak-only  #'),
    ),
    (
        'a placeholder reason ("flaky") is REJECTED',
        lambda: _allowlist_rejects('a.py::test_a::weak-only  # flaky'),
    ),
    (
        'a placeholder reason ("TODO") is REJECTED, case-insensitively',
        lambda: _allowlist_rejects('a.py::test_a::weak-only  # TODO'),
    ),
    (
        'a genuine written reason is ACCEPTED',
        lambda: (
            parse_allowlist_text('a.py::test_a::weak-only  # a real, written reason')
            == {'a.py::test_a::weak-only': ['a real, written reason']}
        ),
    ),
    (
        'a BACKLOG reason is ACCEPTED (deferred is still a real reason)',
        lambda: (
            parse_allowlist_text('a.py::test_a::weak-only  # BACKLOG(#431): tbd for now')
            == {'a.py::test_a::weak-only': ['BACKLOG(#431): tbd for now']}
        ),
    ),
)


def run_selftest(verbose: bool = True) -> list[str]:
    """Return a list of failure descriptions — empty means every detector is alive."""
    failures: list[str] = []

    def check_fires(category: str, source: str, rel: str, label: str) -> None:
        got = {f.category for f in scan_source(source, rel)}
        if category not in got:
            failures.append(f'{label} did not fire (got {sorted(got) or "nothing"})')
        if verbose:
            mark = '\033[31m✗' if category not in got else '\033[32m✓'
            print(f'  {mark}\033[0m fires {label}')

    def check_clean(source: str, rel: str, label: str) -> None:
        found = scan_source(source, rel)
        if found:
            detail = ', '.join(f'{f.category}: {f.detail}' for f in found)
            failures.append(f'{label} produced {detail}')
        if verbose:
            mark = '\033[31m✗' if found else '\033[32m✓'
            print(f'  {mark}\033[0m {label} produces no finding')

    for category, source in SELFTEST_CASES:
        check_fires(category, source, 'fixture.py', category)
    # Path-dependent cases: `external-service-mock`'s weakest claim tier is POSITIONAL
    # (`tests/integration/`, `tests/e2e/`), which a fixture scanned as `fixture.py` cannot
    # express. Without these the tier is unreachable from the self-test and could rot silently.
    for category, rel, source in SELFTEST_PATH_CASES:
        check_fires(category, source, rel, f'{category} @ {rel}')
    for category, rel, source in SELFTEST_ONCE:
        hits = [f for f in scan_source(source, rel) if f.category == category]
        label = f'{category} @ {rel} fires exactly once'
        if len(hits) != 1:
            where = ', '.join(f'{f.test}:{f.line}' for f in hits) or 'nothing'
            failures.append(f'{label}: got {len(hits)} ({where})')
        if verbose:
            mark = '\033[31m✗' if len(hits) != 1 else '\033[32m✓'
            print(f'  {mark}\033[0m {label}')
    for i, source in enumerate(SELFTEST_CLEAN, start=1):
        check_clean(source, 'fixture.py', f'clean case {i}')
    for rel, source in SELFTEST_PATH_CLEAN:
        check_clean(source, rel, f'clean case @ {rel}')
    for label, check in ALLOWLIST_SELFTEST_CASES:
        try:
            ok = check()
        except AllowlistReasonError as exc:
            # An "ACCEPTED" case whose reason the rail wrongly rejects raises here rather
            # than returning False — report it the same way as a failed check.
            ok = False
            failures.append(f'allowlist: {label}: raised {exc!r}')
        else:
            if not ok:
                failures.append(f'allowlist: {label}')
        if verbose:
            mark = '\033[31m✗' if not ok else '\033[32m✓'
            print(f'  {mark}\033[0m allowlist: {label}')
    return failures


def selftest_main() -> int:
    print('\n\033[1maudit-tests self-test\033[0m\n')
    failures = run_selftest()
    if failures:
        print(f'\n\033[31m{len(failures)} self-test failure(s) — a detector is broken\033[0m')
        for line in failures:
            print(f'  {line}')
        print()
        return 1
    total = (
        len(SELFTEST_CASES)
        + len(SELFTEST_PATH_CASES)
        + len(SELFTEST_ONCE)
        + len(SELFTEST_CLEAN)
        + len(SELFTEST_PATH_CLEAN)
        + len(ALLOWLIST_SELFTEST_CASES)
    )
    print(f'\n\033[32mall {total} self-test cases pass\033[0m\n')
    return 0
