"""v449 migration + detection-arm consistency (``speaker.identity_source``, issue #1280)."""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy import Table
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.media import Speaker

REVISION = "v449_add_speaker_identity_source"
PARENT = "v448_task_one_active_row_per_file_step"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_CHECK = "ck_speaker_identity_source"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _scripts():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    return ScriptDirectory.from_config(config)


def test_v449_revision_chain():
    scripts = _scripts()
    assert scripts.get_revision(REVISION).down_revision == PARENT
    heads = sorted(set(scripts.get_heads()))
    assert len(heads) == 1, (
        f"the alembic graph has {len(heads)} heads {heads}; v449 must be the only head "
        f"(down_revision {PARENT!r} in alembic/versions/{REVISION}.py)."
    )


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_the_migration_and_the_model_admit_exactly_the_documented_values():
    from app.models.media import Speaker
    from app.utils.speaker_identity_source import IDENTITY_SOURCES

    def values(check_text: str) -> set[str]:
        return set(re.findall(r"'([a-z_]+)'", check_text.split(" IN ", 1)[1]))

    table = Speaker.__table__
    assert isinstance(table, Table)
    model_check = next(
        c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == _CHECK
    )
    assert values(_module().ADD_CHECK_SQL) == set(IDENTITY_SOURCES)
    assert values(str(model_check.sqltext)) == set(IDENTITY_SOURCES)


def test_the_backfill_record_key_is_the_one_the_application_reads():
    from app.utils.speaker_identity_source import BACKFILL_RECORD_SETTING_KEY

    assert _module().BACKFILL_RECORD_KEY == BACKFILL_RECORD_SETTING_KEY


def test_column_is_nullable_varchar16(db_session):
    row = (
        db_session.connection()
        .execute(
            text(
                "SELECT data_type, character_maximum_length, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = 'speaker' AND column_name = 'identity_source'"
            )
        )
        .one()
    )
    assert tuple(row) == ("character varying", 16, "YES", None)


# --- the CHECK ---------------------------------------------------------------------------


def _speaker_row(db_session, normal_user, **kw):
    from tests.fixtures.speaker_cleanup_seams import make_file
    from tests.fixtures.speaker_cleanup_seams import make_speaker

    return make_speaker(db_session, normal_user, make_file(db_session, normal_user), **kw)


def test_check_rejects_an_unknown_value(db_session, normal_user):
    speaker = _speaker_row(db_session, normal_user)
    conn = db_session.connection()
    with pytest.raises(IntegrityError, match=_CHECK):
        with conn.begin_nested():
            conn.execute(
                text("UPDATE speaker SET identity_source = 'bogus' WHERE id = :i"),
                {"i": speaker.id},
            )


@pytest.mark.parametrize("value", ["voiceprint", "voiceprint_link", "human", None])
def test_check_accepts_every_documented_value(db_session, normal_user, value):
    speaker = _speaker_row(db_session, normal_user)
    conn = db_session.connection()
    conn.execute(
        text("UPDATE speaker SET identity_source = :v WHERE id = :i"),
        {"v": value, "i": speaker.id},
    )
    stored = conn.execute(
        text("SELECT identity_source FROM speaker WHERE id = :i"), {"i": speaker.id}
    ).scalar()
    assert stored == value


# --- the backfill (owner decision Q1) ----------------------------------------------------


_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _shape(
    db_session,
    normal_user,
    *,
    profile_name="Alice",
    created_after_profile=True,
    earlier_carrier=True,
    carrier_created_at=None,
    **overrides,
):
    """A row in the profile auto-accept shape, with ``overrides`` breaking one condition.

    By default the evidence a real auto-accept leaves is present: the profile (created at
    ``_T0``) existed before the speaker, and an earlier speaker was already linked to it.
    ``carrier_created_at`` moves that first carrier in time. ``created_after_profile=False`` makes the speaker older than its profile (what a person
    typing a name produces); ``earlier_carrier=False`` leaves the profile with no other voice.
    """
    from app.models.media import SpeakerProfile
    from tests.fixtures.speaker_cleanup_seams import make_profile

    profile = (
        db_session.query(SpeakerProfile)
        .filter_by(user_id=normal_user.id, organization_id=None, name=profile_name)
        .one_or_none()
    )
    if profile is None:
        profile = make_profile(db_session, normal_user, name=profile_name)
        profile.created_at = _T0
        db_session.flush()
    if earlier_carrier:
        carried = (
            db_session.query(Speaker)
            .filter(Speaker.profile_id == profile.id, Speaker.identity_source == "human")
            .count()
        )
        if not carried:
            _speaker_row(
                db_session,
                normal_user,
                display_name=profile_name,
                verified=True,
                profile_id=profile.id,
                identity_source="human",
                created_at=carrier_created_at or _T0 - timedelta(days=3),
            )
    fields = {
        "display_name": "Alice",
        "suggested_name": "Alice",
        "suggestion_source": "profile_match",
        "confidence": 0.82,
        "verified": True,
        "profile_id": profile.id,
        "created_at": _T0 + timedelta(days=1) if created_after_profile else _T0 - timedelta(days=1),
    }
    fields.update(overrides)
    return _speaker_row(db_session, normal_user, **fields)


def _source(db_session, speaker) -> str | None:
    value: str | None = (
        db_session.connection()
        .execute(text("SELECT identity_source FROM speaker WHERE id = :i"), {"i": speaker.id})
        .scalar()
    )
    return value


def test_backfill_stamps_only_the_profile_auto_accept_shape(db_session, normal_user):
    cases = {
        "the auto-accept shape": (_shape(db_session, normal_user), "voiceprint"),
        "a voice_match accept (identical to an Inbox accept)": (
            _shape(db_session, normal_user, suggestion_source="voice_match"),
            None,
        ),
        "no suggestion left (renamed since)": (
            _shape(db_session, normal_user, suggested_name=None),
            None,
        ),
        "display name is not the profile's": (
            _shape(db_session, normal_user, profile_name="Bob"),
            None,
        ),
        "display name differs from the suggestion": (
            _shape(db_session, normal_user, display_name="Alicia"),
            None,
        ),
        "a different name was auto-accepted, then a person assigned this profile": (
            _shape(db_session, normal_user, suggested_name="Bob"),
            None,
        ),
        "not verified": (_shape(db_session, normal_user, verified=False), None),
        "no profile linked": (_shape(db_session, normal_user, profile_id=None), None),
        "already a person's": (_shape(db_session, normal_user, identity_source="human"), "human"),
    }
    db_session.flush()

    stamped = _module().backfill_and_record(db_session.connection())

    assert stamped >= 1
    got = {label: _source(db_session, sp) for label, (sp, _) in cases.items()}
    assert got == {label: want for label, (_, want) in cases.items()}


def test_a_name_a_person_typed_and_a_later_rematch_rewrote_is_not_stamped(db_session, normal_user):
    """A typed name creates its profile AFTER the speaker; a re-extracted voiceprint then wrote
    the auto-accept fields over it. Stamping it would let a purge delete the person's word."""
    typed_creator = _shape(
        db_session, normal_user, created_after_profile=False, earlier_carrier=False
    )
    typed_creator_with_followers = _shape(
        db_session,
        normal_user,
        profile_name="Carol",
        created_after_profile=False,
        display_name="Carol",
        suggested_name="Carol",
    )
    db_session.flush()

    _module().backfill_and_record(db_session.connection())

    assert _source(db_session, typed_creator) is None
    assert _source(db_session, typed_creator_with_followers) is None


def test_a_genuine_later_auto_accept_is_stamped_and_its_originator_is_not(db_session, normal_user):
    """The same profile: its first carrier (a person's) stays unmarked, the speaker the matcher
    linked to it later is stamped."""
    later = _shape(db_session, normal_user)
    originator = (
        db_session.query(Speaker)
        .filter(Speaker.profile_id == later.profile_id, Speaker.id != later.id)
        .one()
    )
    db_session.flush()

    _module().backfill_and_record(db_session.connection())

    assert _source(db_session, later) == "voiceprint"
    assert _source(db_session, originator) == "human"


def test_a_speaker_older_than_its_profile_is_not_stamped_even_with_an_older_voice_linked(
    db_session, normal_user
):
    """The profile did not exist when this speaker was created, so it did not auto-accept it."""
    older = _shape(db_session, normal_user, created_after_profile=False)
    db_session.flush()

    _module().backfill_and_record(db_session.connection())

    assert _source(db_session, older) is None


@pytest.mark.parametrize("offset", [timedelta(days=1), timedelta(days=2)])
def test_a_voice_linked_at_the_same_time_or_later_is_not_earlier_evidence(
    db_session, normal_user, offset
):
    """Only a speaker that existed BEFORE this one can have contributed the profile's voice."""
    speaker = _shape(db_session, normal_user, carrier_created_at=_T0 + offset)
    db_session.flush()

    _module().backfill_and_record(db_session.connection())

    assert _source(db_session, speaker) is None


def test_a_profile_no_voice_was_linked_to_before_cannot_have_matched_anyone(
    db_session, normal_user
):
    """A match compares against the profile's embedding, which needs an earlier linked speaker."""
    alone = _shape(db_session, normal_user, earlier_carrier=False)
    db_session.flush()

    _module().backfill_and_record(db_session.connection())

    assert _source(db_session, alone) is None


def test_a_speaker_without_a_creation_time_is_never_stamped(db_session, normal_user):
    undated = _shape(db_session, normal_user)
    db_session.connection().execute(
        text("UPDATE speaker SET created_at = NULL WHERE id = :i"), {"i": undated.id}
    )

    _module().backfill_and_record(db_session.connection())

    assert _source(db_session, undated) is None


def test_the_record_counts_the_shape_rows_it_left_unmarked(db_session, normal_user):
    module = _module()
    conn = db_session.connection()
    conn.execute(
        text("DELETE FROM system_settings WHERE key = :k"), {"k": module.BACKFILL_RECORD_KEY}
    )
    _shape(db_session, normal_user)
    _shape(
        db_session,
        normal_user,
        profile_name="Dana",
        display_name="Dana",
        suggested_name="Dana",
        created_after_profile=False,
    )
    _shape(
        db_session,
        normal_user,
        profile_name="Erin",
        display_name="Erin",
        suggested_name="Erin",
        earlier_carrier=False,
    )
    db_session.flush()
    before = conn.execute(text(module.SHAPE_COUNT_SQL)).scalar()

    stamped = module.backfill_and_record(conn)

    record = _record(conn)
    assert record is not None
    assert record["stamped"] == stamped
    assert record["shape_left_unmarked"] == before - stamped
    assert record["shape_left_unmarked"] >= 2


def test_the_upgrade_bounds_its_lock_waits_before_the_first_alter(db_session):
    import ast
    import inspect

    module = _module()
    tree = ast.parse(inspect.getsource(module.upgrade))
    executed = [
        node.args[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and node.args
        and isinstance(node.args[0], ast.Name)
    ]
    assert executed[:3] == ["LOCK_TIMEOUT_SQL", "ADD_COLUMN_SQL", "ADD_CHECK_SQL"]
    conn = db_session.connection()
    conn.execute(text(module.LOCK_TIMEOUT_SQL))
    assert conn.execute(text("SHOW lock_timeout")).scalar() == "30s"


def test_backfill_is_idempotent(db_session, normal_user):
    _shape(db_session, normal_user)
    db_session.flush()
    conn = db_session.connection()
    module = _module()

    assert module.backfill_and_record(conn) >= 1
    assert module.backfill_and_record(conn) == 0


def _record(conn) -> dict | None:
    value = conn.execute(
        text("SELECT value FROM system_settings WHERE key = :k"),
        {"k": _module().BACKFILL_RECORD_KEY},
    ).scalar()
    return json.loads(value) if value is not None else None


def test_the_backfill_count_is_logged_and_recorded_once(db_session, normal_user, caplog):
    module = _module()
    conn = db_session.connection()
    conn.execute(
        text("DELETE FROM system_settings WHERE key = :k"), {"k": module.BACKFILL_RECORD_KEY}
    )
    _shape(db_session, normal_user)
    _shape(db_session, normal_user)
    db_session.flush()

    with caplog.at_level("INFO", logger="alembic.runtime.migration"):
        first = module.backfill_and_record(conn)
    second = module.backfill_and_record(conn)

    assert first >= 2
    assert second == 0
    record = _record(conn)
    assert record is not None
    assert (record["revision"], record["stamped"]) == (REVISION, first)
    assert f"recognised {first} existing speaker" in caplog.text


# --- detection ----------------------------------------------------------------------------


def test_detected_at_or_after_v449(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_check(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE speaker DROP CONSTRAINT {_CHECK}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)


# --- idempotence, both directions ---------------------------------------------------------


@pytest.mark.ddl_exclusive
def test_upgrade_ddl_replays_cleanly(db_session):
    conn = db_session.connection()
    count = "SELECT count(*) FROM pg_constraint WHERE conrelid = 'speaker'::regclass"
    before = conn.execute(text(count)).scalar()
    module = _module()
    for _ in range(2):
        conn.execute(text(module.ADD_COLUMN_SQL))
        conn.execute(text(module.ADD_CHECK_SQL))
    assert conn.execute(text(count)).scalar() == before


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'speaker' AND column_name = 'identity_source')"
            )
        ).scalar()
    )


@pytest.mark.ddl_exclusive
def test_downgrade_is_idempotent_keeps_names_and_upgrade_restores(db_session, normal_user):
    module = _module()
    speaker = _shape(db_session, normal_user)
    db_session.flush()
    conn = db_session.connection()
    module.backfill_and_record(conn)
    try:
        for _ in range(2):
            conn.execute(text(module.DOWNGRADE_SQL))
            conn.execute(text(module.DELETE_RECORD_SQL), {"key": module.BACKFILL_RECORD_KEY})
        assert not _has_column(conn)
        assert _record(conn) is None
        kept = conn.execute(
            text("SELECT display_name, profile_id, verified FROM speaker WHERE id = :i"),
            {"i": speaker.id},
        ).one()
        assert tuple(kept) == ("Alice", speaker.profile_id, True), "names and links survive"

        conn.execute(text(module.ADD_COLUMN_SQL))
        conn.execute(text(module.ADD_CHECK_SQL))
        restamped = module.backfill_and_record(conn)

        assert _has_column(conn)
        assert _source(db_session, speaker) == "voiceprint"
        record = _record(conn)
        assert record is not None and record["stamped"] == restamped >= 1
    finally:
        db_session.rollback()
