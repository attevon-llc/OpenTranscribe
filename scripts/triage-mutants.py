#!/usr/bin/env python3
"""Split surviving mutants into what a caller can observe and what it cannot.

Why this exists
---------------
A raw survivor count is not a work list. `app/auth/lockout.py` reported 171 survivors, and
the honest number of *actionable* ones is a fraction of that: most edit a log message, a
constant that only a log line reads, or the condition guarding a log line. The repo's own
rule is to judge a survivor by whether a real caller could observe the difference — this
applies that rule mechanically instead of by eye, because 171 judgements by eye is where
triage stops happening.

Three categories:

``noise-string``
    The diff changes only text inside a string literal. Nothing but a log reader sees it.

``noise-log-branch``
    The diff changes a **condition** whose body does nothing but log. Flipping
    ``if record.failed_attempts > 0 or locked_until_dt:`` when the body is one
    ``logger.info`` changes which lines appear in a log, not what the function does. My
    first version of this triage called these "REAL logic findings" and inflated the work
    list by ~30%.

``logic``
    Everything else: an operator, a constant, an argument or an assignment that a caller
    could observe. This is the work list.

Usage::

    python3 scripts/triage-mutants.py /tmp/ot-mutation/lockout.log app.auth.lockout
    python3 scripts/triage-mutants.py <log> <dotted-module> --list logic
    python3 scripts/triage-mutants.py --selftest
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from collections import Counter

_STRING = re.compile(r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")
_LOG_CALL = re.compile(r'^\s*(logger|log)\s*\.\s*\w+\s*\(')
_CONDITION = re.compile(r'^\s*(if|elif|while)\b')
#: A comparison or membership test on the changed line means a mutated string is
#: load-bearing rather than cosmetic (`== "x"`, `in (...)`, `.startswith(...)`).
_COMPARISON = re.compile(r'(==|!=|\bin\b|\bis\b|startswith|endswith|<|>)')


def _strip_strings(line: str) -> str:
    return _STRING.sub('<S>', line)


def _is_string_fragment(line: str) -> bool:
    """Is this line nothing but string literal(s) and punctuation?

    That is what a *continuation* line of a multi-line log call looks like:
    ``f"locked until {dt.isoformat()}"``. mutmut mutates such a line to ``None``, so the
    diff is not "text inside a string changed" — the whole expression changed — yet the
    value never leaves the log call. Missing this is what made the first version of this
    triage report every multi-line log message as an actionable finding.
    """
    remainder = _strip_strings(line)
    for token in ('<S>', 'f', ',', '(', ')', '+', '%'):
        remainder = remainder.replace(token, '')
    return remainder.strip() == '' and '<S>' in _strip_strings(line)


def classify(minus: list[str], plus: list[str], body_after: list[str]) -> str:
    """Categorise one mutant from its diff.

    Args:
        minus: the removed lines (without the leading ``-``).
        plus: the added lines.
        body_after: the context lines that FOLLOW the change, used to decide whether a
            mutated condition guards nothing but logging.
    """
    if not minus or not plus:
        return 'unclassified'

    strings_only = [_strip_strings(x) for x in minus] == [_strip_strings(x) for x in plus]

    # A string inside a PREDICATE is not noise — it IS the logic. `_get_pbkdf2_iterations`
    # and `needs_rehash_for_fips_v3` turn on
    # `hashed_password.startswith('$pbkdf2-sha256$')`; mutate that literal and the branch is
    # never taken, so a hash that must be upgraded for FIPS 140-3 silently is not. The
    # strings-equal rule would have filed that as a log-message edit, which is the dangerous
    # direction for a classifier to be wrong in: it hides findings instead of adding work.
    predicate_string = strings_only and (
        _CONDITION.match(minus[0]) or _COMPARISON.search(_strip_strings(minus[0]))
    )
    if strings_only and not predicate_string:
        return 'noise-string'

    # Anything a LOG CALL consumes is unobservable: the argument never leaves the call.
    # Covers both the single-line form (`logger.info("x")` -> `logger.info(None)`) and a
    # continuation line of a multi-line call (`f"locked until ..."` -> `None`).
    if len(minus) == 1 and (_LOG_CALL.match(minus[0]) or _is_string_fragment(minus[0])):
        return 'noise-string'

    # A mutated condition whose body only logs. Look at the lines after the change until
    # the indentation returns to the condition's own level.
    if len(minus) == 1 and _CONDITION.match(minus[0]):
        indent = len(minus[0]) - len(minus[0].lstrip())
        body: list[str] = []
        for line in body_after:
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) <= indent:
                break
            body.append(line)
        # Count STATEMENTS, not lines. This codebase's log calls span several lines
        # (`logger.info(` / f-string / `)`), so `len(body) == 1` silently undid this whole
        # rule for every one of them. A statement is a body line at the body's own minimum
        # indent; anything deeper is a continuation.
        if body:
            base = min(len(b) - len(b.lstrip()) for b in body)
            statements = [
                b
                for b in body
                if len(b) - len(b.lstrip()) == base
                # A lone closing bracket sits at the SAME indent as the call that opened it,
                # so counting it as a statement made every multi-line log call look like two.
                and b.strip().strip(')]},') != ''
            ]
            # Require the log call to be the ONLY statement: a body that logs and then does
            # real work is real work.
            if len(statements) == 1 and _LOG_CALL.match(statements[0]):
                return 'noise-log-branch'

    return 'logic'


def _parse_show(text: str) -> tuple[list[str], list[str], list[str]]:
    """Return (minus, plus, lines-following-the-change) from a ``mutmut show`` diff."""
    minus: list[str] = []
    plus: list[str] = []
    after: list[str] = []
    seen_change = False
    for line in text.splitlines():
        if line.startswith(('---', '+++', '@@', '#')):
            continue
        if line.startswith('-'):
            minus.append(line[1:])
            seen_change = True
        elif line.startswith('+'):
            plus.append(line[1:])
            seen_change = True
        elif seen_change:
            after.append(line[1:] if line.startswith(' ') else line)
    return minus, plus, after


_SELFTEST: list[tuple[str, list[str], list[str], list[str], str]] = [
    (
        'a string literal only',
        ['    logger.info(f"hello {x}")'],
        ['    logger.info(None)'],
        [],
        'noise-string',
    ),
    (
        'a condition guarding only a log call',
        ['        if record.failed_attempts > 0 or locked_until_dt:'],
        ['        if record.failed_attempts > 0 and locked_until_dt:'],
        ['            logger.info("cleared %d", n)', '        record.failed_attempts = 0'],
        'noise-log-branch',
    ),
    (
        'a condition guarding real work is NOT noise',
        ['        if locked_until_dt and now < locked_until_dt:'],
        ['        if locked_until_dt and now <= locked_until_dt:'],
        ['            return True, locked_until_dt'],
        'logic',
    ),
    (
        'an assignment is logic',
        ['    record.failed_attempts = 0'],
        ['    record.failed_attempts = 1'],
        [],
        'logic',
    ),
    (
        'an argument swap is logic',
        ['    x = helper(request, networks)'],
        ['    x = helper(None, networks)'],
        [],
        'logic',
    ),
    (
        'a condition guarding a MULTI-LINE log call is still noise',
        ['        if record.failed_attempts > 0 or locked_until_dt:'],
        ['        if record.failed_attempts > 0 and locked_until_dt:'],
        [
            '            logger.info(',
            '                f"Successful login for {x}, "',
            '                f"clearing {n} failed attempts"',
            '            )',
            '        record.failed_attempts = 0',
        ],
        'noise-log-branch',
    ),
    (
        'a body that logs AND does real work is logic',
        ['        if threshold_reached:'],
        ['        if not threshold_reached:'],
        [
            '            logger.warning("locking")',
            '            record.set_locked_until(unlock_time)',
        ],
        'logic',
    ),
    (
        'a string inside a PREDICATE is logic, not noise',
        ['        if hashed_password.startswith("$pbkdf2-sha256$"):'],
        ['        if hashed_password.startswith("XX$pbkdf2-sha256$XX"):'],
        ['            rounds = int(parts[2])'],
        'logic',
    ),
    (
        'a string compared for equality is logic',
        ['    if payload.get("type") == "access":'],
        ['    if payload.get("type") == "XXaccessXX":'],
        ['        return payload'],
        'logic',
    ),
    ('an empty diff is unclassified', [], [], [], 'unclassified'),
]


def _selftest() -> int:
    failures = 0
    for description, minus, plus, after, expected in _SELFTEST:
        got = classify(minus, plus, after)
        ok = got == expected
        failures += not ok
        print(f'  [{"PASS" if ok else "FAIL"}] {description}  (expected {expected}, got {got})')
    print()
    if failures:
        print(f'{failures} self-test case(s) FAILED — the triage cannot be trusted')
        return 1
    print(f'all {len(_SELFTEST)} self-test cases pass')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('log', nargs='?', help='a run-mutation-tests.sh module log')
    ap.add_argument('module', nargs='?', help='dotted module, e.g. app.auth.lockout')
    ap.add_argument('--list', choices=['logic', 'noise-string', 'noise-log-branch'])
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.log or not args.module:
        ap.error('log and module are required unless --selftest')

    text = pathlib.Path(args.log).read_text(errors='ignore').replace('\r', '\n')
    pattern = re.escape(args.module) + r'\.x[A-Za-z_0-9ǁ]*__mutmut_\d+'
    names = sorted(set(re.findall('(' + pattern + '): survived', text)))
    print(f'{len(names)} surviving mutant(s) in {args.module}', flush=True)

    buckets: dict[str, list[tuple[str, str, str]]] = {}
    for name in names:
        show = subprocess.run(
            ['mutmut', 'show', name], capture_output=True, text=True, timeout=60
        ).stdout
        minus, plus, after = _parse_show(show)
        category = classify(minus, plus, after)
        buckets.setdefault(category, []).append(
            (name, minus[0].strip() if minus else '', plus[0].strip() if plus else '')
        )

    for category in ('logic', 'noise-log-branch', 'noise-string', 'unclassified'):
        items = buckets.get(category, [])
        print(f'  {category:18s} {len(items)}')

    logic = buckets.get('logic', [])
    if logic:
        by_function = Counter(
            re.sub(r'.*\.xǁ?([A-Za-z_0-9ǁ]+)__mutmut_\d+', r'\1', n) for n, _, _ in logic
        )
        print('\nactionable, by function:')
        for function, count in by_function.most_common():
            print(f'  {count:4d}  {function}')

    if args.list:
        print(f'\n--- {args.list} ---')
        for name, old, new in buckets.get(args.list, []):
            print(f'{name}\n   -{old}\n   +{new}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
