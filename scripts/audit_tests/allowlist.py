"""Allowlist parsing and application: one line buys one finding."""

from __future__ import annotations

from pathlib import Path

from audit_tests.findings import Finding

ALLOWLIST_NAME = 'audit-allowlist.txt'


#: Reason prefix marking an entry as DEFERRED WORK rather than an accepted pattern. Counted
#: and printed separately on every run so a backlog cannot masquerade as a clean suite —
#: the failure mode that let `expected-schemas.tsv` rot for four months.
BACKLOG_PREFIX = 'BACKLOG'

#: Reasons that read as "no justification" rather than a real, written one. Exact match only
#: (case-insensitive, trimmed) — a reason that merely CONTAINS one of these words as part of an
#: actual sentence (e.g. "needs a design decision, not a mechanical edit") is not rejected; only
#: a line whose entire reason IS one of these placeholders is. `no reason given` is the sentinel
#: this file used to silently substitute for an empty reason (issue #826) — it is included here
#: so a reason that is literally that sentinel is *also* rejected, not just an empty one.
_NOT_A_REAL_REASON = frozenset(
    {
        'no reason given',
        'todo',
        'tbd',
        'flaky',
        'wip',
        'fixme',
        'n/a',
        'na',
        'none',
    }
)


class AllowlistReasonError(ValueError):
    """An allowlist entry carries no real, written reason — empty or a placeholder.

    The allowlist's whole safeguard is that every accepted/deferred finding was a deliberate,
    reviewed decision. Substituting a placeholder for a missing reason (the previous behaviour)
    made that safeguard decorative: an entry with no justification still passed. This raises
    instead, so a reason-less entry fails the run rather than sitting in the allowlist looking
    reviewed.
    """


def is_real_reason(reason: str) -> bool:
    return bool(reason) and reason.casefold() not in _NOT_A_REAL_REASON


def parse_allowlist_text(text: str, *, source: object = '<text>') -> dict[str, list[str]]:
    """Parse allowlist lines, REJECTING any entry whose reason is not a real one.

    Split out from :func:`load_allowlist` so both the CLI (given a file on disk) and
    ``--selftest`` (given an in-memory string) exercise the identical rejection path — the
    finding in issue #826 was specifically that the file-reading path substituted a placeholder
    instead of refusing, so the two must not diverge again.

    Returns a map of ``<file>::<test>::<category>`` to the reason of EACH line carrying that
    key. A list, not a string, because **one line buys one finding**. Set membership could not
    see a partial fix: ``test_personal_file_read_surfaces_unaffected`` produces three
    ``negated-status`` findings and ``test_community_router_reachable`` produces two, and under
    a set the whole test was exempt as soon as one line existed — fix two of the three
    assertions and the run stayed green with the third still covered. Duplicate keys are
    therefore the encoding of a count, not a mistake to reject; the file already contained
    exactly as many lines as findings for both of those tests.
    """
    allowed: dict[str, list[str]] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, _, reason = line.partition('#')
        key = key.strip()
        reason = reason.strip()
        if not is_real_reason(reason):
            raise AllowlistReasonError(
                f'{source}:{lineno}: allowlist entry for `{key}` has no real reason '
                f'(got {reason!r}). A written reason is mandatory — see the header of '
                f'{ALLOWLIST_NAME}.'
            )
        allowed.setdefault(key, []).append(reason)
    return allowed


def load_allowlist(root: Path) -> dict[str, list[str]]:
    """Load and parse ``<root>/audit-allowlist.txt``, or ``{}`` if it does not exist."""
    path = root / ALLOWLIST_NAME
    if not path.exists():
        return {}
    return parse_allowlist_text(path.read_text(), source=path)


def apply_allowlist(
    findings: list[Finding], allowed: dict[str, list[str]]
) -> tuple[list[Finding], list[Finding], int, list[str]]:
    """Split findings by allowlist coverage, one line per finding.

    Returns ``(unallowlisted, backlog, accepted, stale)``. ``stale`` describes entries with no
    finding left to cover — either the key vanished entirely or the file holds MORE lines than
    the key now produces, which is the partial-fix signal the set-based version could not give.
    """
    by_key: dict[str, list[Finding]] = {}
    for finding in findings:
        by_key.setdefault(finding.key, []).append(finding)

    unallowlisted: list[Finding] = []
    backlog: list[Finding] = []
    accepted = 0
    for key, group in by_key.items():
        reasons = allowed.get(key, [])
        # Pairing is positional and the scan order is deterministic, so which line covers
        # which occurrence is stable across runs. It does not matter WHICH: the reasons for a
        # repeated key describe the same defect, and the count is the thing being enforced.
        for finding, reason in zip(group, reasons, strict=False):
            if reason.startswith(BACKLOG_PREFIX):
                backlog.append(finding)
            else:
                accepted += 1
        unallowlisted.extend(group[len(reasons) :])

    stale: list[str] = []
    for key, reasons in sorted(allowed.items()):
        live = len(by_key.get(key, []))
        if live >= len(reasons):
            continue
        if live == 0:
            stale.append(
                f'{key}  ({len(reasons)} entr{"y" if len(reasons) == 1 else "ies"}, 0 findings)'
            )
        else:
            stale.append(
                f'{key}  ({len(reasons)} entries, {live} finding(s) — delete {len(reasons) - live})'
            )
    return unallowlisted, backlog, accepted, stale
