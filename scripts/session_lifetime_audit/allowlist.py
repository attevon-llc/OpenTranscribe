"""The reviewed-exemption allowlist: loading, count-aware matching and the BACKLOG split."""

from __future__ import annotations

from pathlib import Path

from session_lifetime_audit.findings import Finding

ALLOWLIST_PATH = Path(__file__).resolve().parent.parent / 'session-lifetime-allowlist.txt'

#: Reason prefix marking an entry as DEFERRED WORK rather than an accepted pattern.
BACKLOG_PREFIX = 'BACKLOG'


def load_allowlist(path: Path = ALLOWLIST_PATH) -> dict[str, list[str]]:
    """Map ``<file>::<scope>::<category>`` to the reason of EACH line carrying that key.

    A list, not a set: **one line buys one finding**. Set membership cannot see a PARTIAL
    fix — a key producing three findings and a key producing one would be the same key, so
    fixing two of three calls would leave the run green with the exemption still covering
    the third. Duplicate keys are therefore meaningful, not an error.
    """
    if not path.exists():
        return {}
    allowed: dict[str, list[str]] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, _, reason = line.partition('#')
        key = key.strip()
        if not key:
            continue
        allowed.setdefault(key, []).append(reason.strip())
    return allowed


def apply_allowlist(
    findings: list[Finding], allowed: dict[str, list[str]]
) -> tuple[list[Finding], list[Finding], int, list[str], list[str]]:
    """Split findings by allowlist coverage, one line per finding.

    Returns ``(unallowlisted, backlog, accepted, stale, unreasoned)``. ``stale`` names entries
    with no finding left to cover — either the key vanished or the file holds MORE lines than
    the key now produces. That is what makes "this file can only shrink" true per-finding
    rather than per-function. ``unreasoned`` names entries whose reason is missing: an
    exemption nobody had to justify is the one nobody will ever revisit.
    """
    by_key: dict[str, list[Finding]] = {}
    for finding in findings:
        by_key.setdefault(finding.key, []).append(finding)

    unallowlisted: list[Finding] = []
    backlog: list[Finding] = []
    accepted = 0
    for key, group in by_key.items():
        reasons = allowed.get(key, [])
        for _finding, reason in zip(group, reasons, strict=False):
            if reason.startswith(BACKLOG_PREFIX):
                backlog.append(_finding)
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

    unreasoned = sorted(key for key, reasons in allowed.items() if any(not r for r in reasons))
    return unallowlisted, backlog, accepted, stale, unreasoned
