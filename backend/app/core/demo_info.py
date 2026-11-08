"""What a public demo reports about itself to its visitors (issue #628, follow-up P-B).

Pure helpers: the next reset instant and the last-upgrade time. The reset schedule is the
systemd timer in ``scripts/demo/systemd/opentranscribe-demo-reset.timer``
(``tests/unit/test_demo_info.py`` pins this constant to that file); the upgrade time is read
from the ``demo-status.json`` that ``scripts/demo/demo-upgrade.sh`` writes and the demo
overlay mounts read-only at ``DEMO_STATUS_FILE``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

#: ``MediaFile.metadata_important`` key the seeder writes per seeded item and
#: ``GET /auth/demo/attributions`` reads back.
DEMO_LICENSE_KEY = "demo_license"

#: Hour (UTC) of the nightly reset.
DEMO_RESET_HOUR_UTC = 4

#: The only ``result`` values in ``demo-status.json`` that mean "now running this tag".
_UPGRADED_RESULT = "upgraded"


def next_reset_at(now: datetime) -> datetime:
    """The next nightly reset strictly after ``now`` (an aware datetime)."""
    now = now.astimezone(UTC)
    candidate = now.replace(hour=DEMO_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def last_upgrade_at(status_file: str) -> datetime | None:
    """When the demo last moved to its current release, or ``None`` if unknown.

    Unknown covers every non-success: no path configured, file absent, unreadable, not the
    expected shape, or a rolled-back/failed result. A visitor-facing line must never claim
    an upgrade the host did not record.
    """
    if not status_file:
        return None
    try:
        data = json.loads(Path(status_file).read_text(encoding="utf-8"))
        if data.get("result") != _UPGRADED_RESULT:
            return None
        stamp = datetime.fromisoformat(str(data["at"]))
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        logger.info("demo status file unreadable: %s", type(exc).__name__)
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
