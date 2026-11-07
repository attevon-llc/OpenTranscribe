"""Schema detection, v463 onward: the data-safety cluster (trash, hide, scheduled erasure, owner).

v461 closed the governance era (``governance.py``); this era builds on its top rung
(``governance.matches_v462``). ``probe()`` issues this era's queries after every older era's;
``ARMS`` is this era's slice of the newest-first ladder (``ladder.py``), and its first arm is the
top of it.

A rung requires every earlier probe of this era except an index the runner builds concurrently
after the upgrade: v463's ``ix_media_file_trash_due`` is not a probe at all, its detection keys on
``ck_media_file_trash_state``, which the revision itself creates last.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from app.db.schema_detect.governance import matches_v462
from app.db.schema_detect.probe import check_exists

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from app.db.schema_detect.ladder import Arm
    from app.db.schema_detect.ladder import SchemaFacts


@dataclass(frozen=True)
class DataSafetyFacts:
    """Probe answers for v463 onward."""

    has_media_file_trash: bool


def probe(conn: Connection, tables: list[str]) -> DataSafetyFacts:
    """Run this era's probes against *conn*, in ladder order."""
    del tables  # every probe here reads pg_constraint, which always exists
    _check_exists = partial(check_exists, conn)
    return DataSafetyFacts(
        # v463: the trash state on media_file (issue #1101). Keyed on the all-or-nothing CHECK,
        # the last thing the revision creates.
        has_media_file_trash=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_file_trash_state')"
        ),
    )


def matches_v463(f: SchemaFacts) -> bool:
    return matches_v462(f) and f.data_safety.has_media_file_trash


ARMS: tuple[Arm, ...] = ((matches_v463, "v463_add_media_file_trash"),)
