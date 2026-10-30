"""Backup filename pattern, local-directory listing and GFS retention pruning."""

from __future__ import annotations

import logging
import re
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Filenames we own. ``-Fc`` custom-format dumps + their optional gpg envelope.
_DUMP_PREFIX = "opentranscribe-"
_DUMP_SUFFIX = ".dump"
_GPG_SUFFIX = ".dump.gpg"
# opentranscribe-YYYYMMDD-HHMMSS.dump[.gpg]
_TS_RE = re.compile(r"opentranscribe-(\d{8})-(\d{6})\.dump(?:\.gpg)?$")


def _backup_timestamp(name: str) -> datetime | None:
    """Parse the embedded timestamp from one of our backup filenames."""
    m = _TS_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)}{m.group(2)}", "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def list_backups(destination: str) -> list[dict[str, Any]]:
    """List backup files in the destination, newest first."""
    path = Path(destination)
    if not path.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in path.iterdir():
        if not entry.is_file():
            continue
        ts = _backup_timestamp(entry.name)
        if ts is None:
            continue
        stat = entry.stat()
        out.append(
            {
                "filename": entry.name,
                "size_bytes": stat.st_size,
                "created_at": ts.isoformat(),
                "encrypted": entry.name.endswith(_GPG_SUFFIX),
            }
        )
    out.sort(key=lambda b: b["created_at"], reverse=True)
    return out


# =============================================================================
# GFS retention pruning (pure-python, tested)
# =============================================================================
def select_backups_to_delete(
    filenames: list[str],
    *,
    retention_daily: int,
    retention_weekly: int,
    retention_monthly: int,
) -> list[str]:
    """Return the subset of ``filenames`` to delete under a GFS policy.

    Grandfather-father-son: keep the N most-recent dailies, then N weeklies (one per
    ISO week, the most recent in each), then N monthlies (one per calendar month). A
    backup retained by any tier survives. Filenames that don't match our pattern are
    ignored (never deleted). Pure function — caller does the unlinking.
    """
    dated = [(name, ts) for name in filenames if (ts := _backup_timestamp(name)) is not None]
    dated.sort(key=lambda x: x[1], reverse=True)  # newest first

    keep: set[str] = set()

    # Daily: the N most-recent backups overall.
    for name, _ in dated[: max(retention_daily, 0)]:
        keep.add(name)

    # Weekly: most-recent backup per ISO (year, week), newest N weeks.
    seen_weeks: dict[tuple[int, int], str] = {}
    for name, ts in dated:
        key = ts.isocalendar()[:2]
        if key not in seen_weeks:
            seen_weeks[key] = name
    for key in sorted(seen_weeks, reverse=True)[: max(retention_weekly, 0)]:
        keep.add(seen_weeks[key])

    # Monthly: most-recent backup per (year, month), newest N months.
    seen_months: dict[tuple[int, int], str] = {}
    for name, ts in dated:
        key = (ts.year, ts.month)
        if key not in seen_months:
            seen_months[key] = name
    for key in sorted(seen_months, reverse=True)[: max(retention_monthly, 0)]:
        keep.add(seen_months[key])

    return [name for name, _ in dated if name not in keep]


def prune_backups(destination: str, settings_dict: dict[str, Any]) -> list[str]:
    """Delete old backups in ``destination`` per the GFS retention settings.

    Returns the list of deleted filenames.
    """
    path = Path(destination)
    if not path.is_dir():
        return []
    filenames = [b["filename"] for b in list_backups(destination)]
    to_delete = select_backups_to_delete(
        filenames,
        retention_daily=int(settings_dict["retention_daily"]),
        retention_weekly=int(settings_dict["retention_weekly"]),
        retention_monthly=int(settings_dict["retention_monthly"]),
    )
    deleted: list[str] = []
    for name in to_delete:
        try:
            (path / name).unlink()
            deleted.append(name)
        except OSError as exc:
            logger.warning("Could not delete old backup %s: %s", name, exc)
    return deleted
