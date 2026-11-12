"""Minimal 5-field cron parsing + due-check for scheduled backups (no croniter dependency).

Pure functions, shared by the backup, media-mirror and directory-sync schedulers.
"""

from __future__ import annotations

import itertools
from datetime import UTC
from datetime import datetime
from datetime import timedelta


# =============================================================================
# Cron parsing + due check (minimal, no croniter dependency)
# =============================================================================
def _parse_field(field: str, low: int, high: int) -> set[int]:
    """Expand one cron field (``*``, ``a-b``, ``*/n``, ``a,b``) into a value set."""
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        token = part
        if "/" in part:
            token, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"Invalid step in cron field: {part!r}")
        if token in ("*", ""):
            start, end = low, high
        elif "-" in token:
            start_s, end_s = token.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(token)
        if start < low or end > high or start > end:
            raise ValueError(f"Cron field out of range: {part!r} (expected {low}-{high})")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse a 5-field cron expression into per-field value sets.

    Fields: minute hour day-of-month month day-of-week (0=Sunday).
    Raises ``ValueError`` on a malformed expression.
    """
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Cron must have 5 fields, got {len(parts)}: {expr!r}")
    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12)
    dow = _parse_field(parts[4], 0, 6)
    return minute, hour, dom, month, dow


def is_valid_cron(expr: str) -> bool:
    """Return True if ``expr`` is a parseable 5-field cron expression."""
    try:
        parse_cron(expr)
        return True
    except (ValueError, TypeError):
        return False


def _matches(dt: datetime, cron: tuple[set[int], set[int], set[int], set[int], set[int]]) -> bool:
    """Return True if ``dt`` (minute resolution) satisfies the cron expression.

    Day matching follows Vixie cron: when both day-of-month and day-of-week are
    restricted (not ``*``), the minute fires if EITHER matches.
    """
    minute, hour, dom, month, dow = cron
    if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
        return False
    # Python weekday(): Mon=0..Sun=6 → cron dow Sun=0..Sat=6
    cron_dow = (dt.weekday() + 1) % 7
    dom_restricted = dom != set(range(1, 32))
    dow_restricted = dow != set(range(7))
    if dom_restricted and dow_restricted:
        return dt.day in dom or cron_dow in dow
    return dt.day in dom and cron_dow in dow


# A never-run schedule is caught up if its cron minute fell within this window. It must exceed
# the slowest beat tick interval (directory sync ticks every 15 min) so some tick always lands
# inside it, yet stay short so enabling a schedule does not fire a stale occurrence at once.
FIRST_RUN_GRACE = timedelta(minutes=30)


def is_due(schedule: str, last_run_at: str | None, now: datetime | None = None) -> bool:
    """Decide whether a scheduled job should run at ``now`` given the last run time.

    Fires if any cron-matching minute falls in the window ``(last_run_at, now]``. Callers stamp
    ``last_run_at`` when they dispatch, so a given occurrence fires once. A schedule that has
    never run (``last_run_at`` None or unparseable) uses ``(now - FIRST_RUN_GRACE, now]``: any
    beat tick shortly after the cron minute catches it, rather than only a tick landing exactly
    on that minute. ``now`` defaults to the current UTC time (truncated to minute).
    """
    cron = parse_cron(schedule)
    now = (now or datetime.now(UTC)).replace(second=0, microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    last: datetime | None = None
    if last_run_at:
        try:
            last = datetime.fromisoformat(last_run_at)
        except (ValueError, TypeError):
            last = None
    if last is None:
        last = now - FIRST_RUN_GRACE
    elif last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    last = last.replace(second=0, microsecond=0)

    if last >= now:
        return False
    # Walk minute-by-minute through the gap; cap the scan so a stale/old last_run
    # (e.g. weeks of downtime) can't loop forever — one matching minute is enough.
    cursor = last + timedelta(minutes=1)
    scanned = 0
    while cursor <= now and scanned < 44640:  # 31 days of minutes
        if _matches(cursor, cron):
            return True
        cursor += timedelta(minutes=1)
        scanned += 1
    return False


#: Horizon scanned by :func:`max_interval`; two months covers every monthly schedule.
_INTERVAL_SCAN_DAYS = 62


def max_interval(schedule: str, start: datetime | None = None) -> timedelta:
    """Return the longest gap between consecutive fires of ``schedule``.

    Scans ``_INTERVAL_SCAN_DAYS`` minute by minute from ``start`` (default now, UTC).
    Raises ``ValueError`` for a malformed cron or one that fires fewer than twice in the
    horizon (so no interval exists).
    """
    cron = parse_cron(schedule)
    cursor = (start or datetime.now(UTC)).replace(second=0, microsecond=0)
    fires = []
    for _ in range(_INTERVAL_SCAN_DAYS * 1440):
        if _matches(cursor, cron):
            fires.append(cursor)
        cursor += timedelta(minutes=1)
    if len(fires) < 2:
        raise ValueError(f"Cron fires fewer than twice in {_INTERVAL_SCAN_DAYS} days: {schedule!r}")
    return max(b - a for a, b in itertools.pairwise(fires))
