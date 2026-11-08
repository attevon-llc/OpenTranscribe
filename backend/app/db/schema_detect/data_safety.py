"""Schema detection, v463 onward: the data-safety cluster (trash, hide, scheduled erasure, holds) and v469's audio tracks.

v461 closed the governance era (``governance.py``); this era builds on its top rung
(``governance.matches_v462``). ``probe()`` issues this era's queries after every older era's;
``ARMS`` is this era's slice of the newest-first ladder (``ladder.py``), and its first arm is the
top of it.

A rung requires every earlier probe of this era except an index the runner builds concurrently
after the upgrade: v463's ``ix_media_file_trash_due`` and v467's ``ix_content_hold_platform_active``
are not probes at all; their detection keys on a CHECK the revision itself creates last.
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
    has_erasure_grace_subject_index: bool
    has_user_hidden_media: bool
    has_content_hold_authority: bool
    has_organization_owner: bool
    has_media_file_audio_streams: bool
    has_requested_source_language: bool


def probe(conn: Connection, tables: list[str]) -> DataSafetyFacts:
    """Run this era's probes against *conn*, in ladder order."""
    del tables  # every probe here reads a pg catalog, which always exists
    _check_exists = partial(check_exists, conn)
    return DataSafetyFacts(
        # v463: the trash state on media_file (issue #1101). Keyed on the all-or-nothing CHECK,
        # the last thing the revision creates.
        has_media_file_trash=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_file_trash_state')"
        ),
        # v464: one open scheduled erasure per subject (issue #1105). Keyed on the index it
        # creates; v455's probe accepts it too, because v464 drops v455's index.
        has_erasure_grace_subject_index=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE tablename = 'erasure_ledger' "
            "AND indexname = 'uq_erasure_ledger_open_grace_subject')"
        ),
        # v465: per-user hide of media files (issue #1102). Keyed on the primary key the
        # CREATE TABLE names; a catalog probe, so a database without the table answers False.
        has_user_hidden_media=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'pk_user_hidden_media')"
        ),
        # v467: who placed a content hold (organization or platform). Keyed on the CHECK the
        # revision creates last; v467's partial index is built after the upgrade, never probed.
        has_content_hold_authority=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_content_hold_authority_org')"
        ),
        # v468: the organization's owner. Keyed on the FK the revision creates last.
        has_organization_owner=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'fk_organization_owner_user')"
        ),
        # v469: the audio tracks of a media file and the chosen one (issue #1263). Keyed on the
        # CHECK the revision creates last.
        has_media_file_audio_streams=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_media_file_audio_stream_index')"
        ),
        # v470: the language forced for one run (issue #1263). Keyed on its CHECK.
        has_requested_source_language=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_media_file_requested_source_language')"
        ),
    )


def matches_v463(f: SchemaFacts) -> bool:
    return matches_v462(f) and f.data_safety.has_media_file_trash


def matches_v464(f: SchemaFacts) -> bool:
    return matches_v463(f) and f.data_safety.has_erasure_grace_subject_index


def matches_v465(f: SchemaFacts) -> bool:
    return matches_v464(f) and f.data_safety.has_user_hidden_media


def matches_v467(f: SchemaFacts) -> bool:
    return matches_v465(f) and f.data_safety.has_content_hold_authority


def matches_v468(f: SchemaFacts) -> bool:
    return matches_v467(f) and f.data_safety.has_organization_owner


def matches_v469(f: SchemaFacts) -> bool:
    return matches_v468(f) and f.data_safety.has_media_file_audio_streams


def matches_v470(f: SchemaFacts) -> bool:
    return matches_v469(f) and f.data_safety.has_requested_source_language


ARMS: tuple[Arm, ...] = (
    (matches_v470, "v470_add_requested_source_language"),
    (matches_v469, "v469_add_media_file_audio_streams"),
    (matches_v468, "v468_add_organization_owner"),
    (matches_v467, "v467_add_content_hold_authority"),
    (matches_v465, "v465_add_user_hidden_media"),
    (matches_v464, "v464_generalise_erasure_grace_index"),
    (matches_v463, "v463_add_media_file_trash"),
)
