"""v385 migration + detection-arm consistency (drop three orphan tables).

Same shape as the other per-revision suites, with one difference that matters:
v385 **removes** objects rather than adding them, so every fingerprint here is an
absence. That inverts the usual detection logic — `_detect_schema_version` must
recognise a v385 database by what is *gone*, and a database still carrying any of
the three tables must stamp lower.

Issue #398.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v385_drop_orphan_tables"
ORPHAN_TABLES = ("upload_session", "speaker_audio_clip", "user_certificate_preferences")

#: `ddl_exclusive` is applied PER TEST below, never to the module. An EXCLUSIVE advisory-lock
#: acquisition drains every other xdist worker, so spending one on a read-only schema
#: assertion turns that assertion into a full-suite barrier — that is what made this group
#: 414 s of a 511 s wall clock. Only the tests that actually execute ALTER/DROP/CREATE carry
#: it; the lock's EXCLUSIVE mode already serialises them against each other across workers,
#: so `xdist_group` is not needed on top (issue #389, #431).
#: Both directions are enforced by `tests/unit/test_ddl_marker_discipline.py`.


def test_v385_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v384_add_chat_reasoning_content"

    heads = set(scripts.get_heads())
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_orphan_tables_are_gone(db_session):
    """The whole point of the revision."""
    conn = db_session.connection()
    present = [t for t in ORPHAN_TABLES if inspect(conn).has_table(t)]
    assert not present, (
        f"v385 should have dropped {present}; they are unreferenced by any model, query, or script"
    )


def test_the_drop_is_idempotent():
    """Uses IF EXISTS, so a partially-migrated database can re-run it.

    backend/alembic/CLAUDE.md requires this: the startup runner stamps untracked
    databases by fingerprint, so a revision routinely re-runs against a database
    that already has part of its changes applied.
    """
    source = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
    ).read_text()
    for table in ORPHAN_TABLES:
        assert table in source
    assert "IF EXISTS" in source, "the drop must tolerate an already-dropped table"


def test_downgrade_is_an_explicit_no_op():
    """The downgrade must not pretend to restore what it cannot.

    Recreating an empty table from a column list that no longer describes any
    running code would look like a restore and be nothing of the kind. The
    revision documents that choice rather than leaving an empty function.
    """
    source = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
    ).read_text()
    downgrade = source.split("def downgrade()", 1)[1]
    assert "no-op" in downgrade.lower()
    # A docstring, not a bare `pass` — the reasoning is the deliverable here.
    assert '"""' in downgrade


def test_tag_created_at_was_adopted_not_dropped(db_session):
    """The fourth #398 finding was fixed in the MODEL, not by dropping a column.

    `tag.created_at` has existed since v230_add_auto_labeling and carries a
    server default. Dropping a populated timestamptz to satisfy a drift
    comparison would have been the wrong direction; the model now declares it.
    """
    from app.models.media import Tag

    conn = db_session.connection()
    columns = {c["name"] for c in inspect(conn).get_columns("tag")}
    assert "created_at" in columns, "the column must still exist"
    assert "created_at" in Tag.__table__.columns, "the model must now declare it"


def test_detection_recognizes_a_v385_database(db_session):
    """The absence fingerprint must not stamp lower than v385.

    Step 4 of the 5-step procedure in backend/app/db/CLAUDE.md — the step that
    gets skipped, and skipping it silently mis-stamps untracked databases.

    The comparison goes through ``_migration_detection.assert_detected_at_or_after``, which
    compares **positions in the alembic chain**. The string form this replaced
    (``detected >= REVISION``) was lexicographic: it holds today only because every
    revision id happens to be three digits, and would silently invert the day one is not —
    ``"v3100_…" < "v385_…"``, so a *newer* revision would read as older and the test would
    fail while the ladder was correct.
    """
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_when_an_orphan_table_is_present(db_session):
    """Re-create one orphan table and the fingerprint must stop matching v385.

    This is the half that proves the arm is actually wired to the probe rather
    than passing for some unrelated reason.
    """
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    conn.execute(text("CREATE TABLE IF NOT EXISTS upload_session (id INTEGER PRIMARY KEY)"))

    tables = inspect(conn).get_table_names()
    detected = _detect_schema_version(conn, tables)

    conn.execute(text("DROP TABLE IF EXISTS upload_session"))

    assert detected != REVISION, (
        "a database still carrying upload_session must not be stamped v385 — "
        "the detection arm is not reading the orphan-table probe"
    )
